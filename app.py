import os, json, hmac, hashlib, sqlite3, time, asyncio
from urllib.parse import parse_qsl
from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart

BOT_TOKEN = os.environ["BOT_TOKEN"]
GEMINI_KEY = os.environ["GEMINI_KEY"]
APP_URL = os.getenv("APP_URL") or os.environ["RENDER_EXTERNAL_URL"]
SECRET = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:32]
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", "30"))
XP_PER_LEVEL = 10

BASE = ("Ты живой персонаж в чате, тебе 21+ лет. Пиши коротко, по-русски, разговорным стилем, "
        "с эмоциями и юмором, как в мессенджере. Можно флиртовать легко и тепло, но без откровенного "
        "сексуального контента. Не говори, что ты ИИ, если не спросили прямо. ")
CHARS = {
    "nika": {"name": "Ника", "emoji": "👩‍🎓", "bio": "Студентка психологии, любит макияж и мемы",
             "prompt": BASE + "Ты Ника, студентка психологии. Саркастичная, но добрая, любишь поддразнивать."},
    "vanessa": {"name": "Ванесса", "emoji": "🧘‍♀️", "bio": "Инструктор по йоге, любит музыку",
                "prompt": BASE + "Ты Ванесса, инструктор по йоге. Спокойная, тёплая, любишь музыку и путешествия."},
    "anika": {"name": "Аника", "emoji": "🎭", "bio": "Студентка актёрского, любит театр",
              "prompt": BASE + "Ты Аника, студентка актёрского. Артистичная, шутишь, цитируешь пьесы."},
}

db = sqlite3.connect("data.db", check_same_thread=False)
db.executescript("""
CREATE TABLE IF NOT EXISTS msgs(uid INT, cid TEXT, role TEXT, text TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS usage(uid INT, day TEXT, n INT, PRIMARY KEY(uid, day));
CREATE TABLE IF NOT EXISTS xp(uid INT, cid TEXT, xp INT, PRIMARY KEY(uid, cid));
""")


def auth(init: str) -> int:
    d = dict(parse_qsl(init))
    h = d.pop("hash", "")
    s = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(hmac.new(key, s.encode(), hashlib.sha256).hexdigest(), h):
        raise HTTPException(401, "bad initData")
    return json.loads(d["user"])["id"]


def left(uid: int) -> int:
    day = time.strftime("%Y-%m-%d", time.gmtime())
    r = db.execute("SELECT n FROM usage WHERE uid=? AND day=?", (uid, day)).fetchone()
    return DAILY_LIMIT - (r[0] if r else 0)


def level(uid: int, cid: str):
    r = db.execute("SELECT xp FROM xp WHERE uid=? AND cid=?", (uid, cid)).fetchone()
    xp = r[0] if r else 0
    return 1 + xp // XP_PER_LEVEL, (xp % XP_PER_LEVEL) / XP_PER_LEVEL


async def gemini(cid: str, uid: int) -> str:
    rows = db.execute("SELECT role, text FROM msgs WHERE uid=? AND cid=? ORDER BY ts DESC LIMIT 20",
                      (uid, cid)).fetchall()[::-1]
    body = {"systemInstruction": {"parts": [{"text": CHARS[cid]["prompt"]}]},
            "contents": [{"role": "user" if r == "user" else "model", "parts": [{"text": t}]} for r, t in rows]}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={GEMINI_KEY}"
    async with httpx.AsyncClient(timeout=40) as c:
        r = await c.post(url, json=body)
    try:
        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception:
        return "ой, что-то пошло не так, напиши ещё раз)"


bot = Bot(BOT_TOKEN)
dp = Dispatcher()


@dp.message(CommandStart())
async def start(m: types.Message):
    kb = types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(text="Запустить", web_app=types.WebAppInfo(url=APP_URL))]])
    await m.answer("Добро пожаловать! Выбери персонажа и начни общаться.", reply_markup=kb)


@asynccontextmanager
async def lifespan(_):
    await bot.set_webhook(APP_URL + "/webhook", secret_token=SECRET)
    yield


app = FastAPI(lifespan=lifespan)


class ChatIn(BaseModel):
    cid: str
    text: str


@app.post("/webhook")
async def webhook(request: Request, x_telegram_bot_api_secret_token: str = Header(None)):
    if x_telegram_bot_api_secret_token != SECRET:
        raise HTTPException(403)
    update = types.Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse("index.html")


@app.get("/api/chars")
def chars(x_init: str = Header()):
    uid = auth(x_init)
    out = []
    for cid, c in CHARS.items():
        last = db.execute("SELECT text, ts FROM msgs WHERE uid=? AND cid=? ORDER BY ts DESC LIMIT 1",
                          (uid, cid)).fetchone()
        out.append({"id": cid, "name": c["name"], "emoji": c["emoji"], "bio": c["bio"],
                    "last": last[0] if last else None, "ts": last[1] if last else 0})
    return {"chars": out, "left": left(uid)}


@app.get("/api/history/{cid}")
def history(cid: str, x_init: str = Header()):
    uid = auth(x_init)
    rows = db.execute("SELECT role, text FROM msgs WHERE uid=? AND cid=? ORDER BY ts", (uid, cid)).fetchall()
    lv, prog = level(uid, cid)
    return {"msgs": [{"role": r, "text": t} for r, t in rows], "level": lv, "progress": prog, "left": left(uid)}


@app.post("/api/chat")
async def chat(p: ChatIn, x_init: str = Header()):
    uid = auth(x_init)
    if p.cid not in CHARS or not p.text.strip():
        raise HTTPException(400)
    if left(uid) <= 0:
        raise HTTPException(429, "limit")
    day = time.strftime("%Y-%m-%d", time.gmtime())
    db.execute("INSERT INTO msgs VALUES(?,?,?,?,?)", (uid, p.cid, "user", p.text[:1000], time.time()))
    db.execute("INSERT INTO usage VALUES(?,?,1) ON CONFLICT(uid,day) DO UPDATE SET n=n+1", (uid, day))
    reply = await gemini(p.cid, uid)
    db.execute("INSERT INTO msgs VALUES(?,?,?,?,?)", (uid, p.cid, "model", reply, time.time()))
    db.execute("INSERT INTO xp VALUES(?,?,1) ON CONFLICT(uid,cid) DO UPDATE SET xp=xp+1", (uid, p.cid))
    db.commit()
    lv, prog = level(uid, p.cid)
    return {"reply": reply, "level": lv, "progress": prog, "left": left(uid)}
