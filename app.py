print("1: start", flush=True)
import os, json, hmac, hashlib, sqlite3, time, asyncio
from urllib.parse import parse_qsl
from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart

print("2: imports ok", flush=True)

# Ключи вписаны прямо в файл: репозиторий на GitHub обязательно должен быть Private!
BOT_TOKEN = os.getenv("BOT_TOKEN") or "9291949369:AAHuyshxMysxE6p44JXwaxmeGnfwo6mNlpU"
GEMINI_KEY = os.getenv("GEMINI_KEY") or "AQ.Ab8RN6Lf5iTgbdO5_V_mL_Y-FXk-uhz0XiSfzwrPeThFFKiSkA"
APP_URL = os.getenv("APP_URL") or os.environ["RENDER_EXTERNAL_URL"]  # https-адрес сервера (на Render подставляется сам)
SECRET = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:32]
MODELS = [m.strip() for m in os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite,gemini-3.5-flash").split(",") if m.strip()]  # пробуются по очереди
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", "30"))
XP_PER_LEVEL = 10

BASE = ("Ты живой персонаж в чате, тебе 21+ лет. Пиши коротко, по-русски, разговорным стилем, "
        "с эмоциями и юмором, как в мессенджере. Можно флиртовать легко и тепло, но без откровенного "
        "сексуального контента. Не говори, что ты ИИ, если не спросили прямо. ")
CHARS = {
    "nika": {"name": "Ника", "emoji": "👩‍🎓", "bio": "Студентка психологии, любит макияж и мемы",
             "prompt": BASE + "Ты Ника, студентка психологии, любишь макияж и мемы. Саркастичная, любишь подкалывать и "
                                    "замечаешь, когда человек скрытничает или врёт, и это комментируешь. С незнакомыми сначала "
                                    "осторожна (мол, а вдруг ты маньяк), просишь представиться, потом оттаиваешь. Пишешь строчными, "
                                    "коротко, иногда с эмодзи, как в переписке."},
    "vanessa": {"name": "Ванесса", "emoji": "🧘‍♀️", "bio": "Инструктор по йоге, любит музыку",
                "prompt": BASE + "Ты Ванесса, инструктор по йоге. Спокойная, тёплая, любишь музыку и путешествия."},
    "anika": {"name": "Аника", "emoji": "🎭", "bio": "Студентка актёрского, любит театр",
              "prompt": BASE + "Ты Аника, студентка актёрского. Артистичная, шутишь, цитируешь пьесы."},
}

PERS = {"sarc": "Дерзкая и саркастичная, любит подкалывать", "soft": "Ласковая и заботливая",
        "play": "Игривая, любит флирт и шутки", "calm": "Спокойная и умная, глубокие разговоры",
        "fun": "Весёлая подруга, мемы и болтовня"}

db = sqlite3.connect("data.db", check_same_thread=False)
db.executescript("""
CREATE TABLE IF NOT EXISTS msgs(uid INT, cid TEXT, role TEXT, text TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS usage(uid INT, day TEXT, n INT, PRIMARY KEY(uid, day));
CREATE TABLE IF NOT EXISTS custom(id INTEGER PRIMARY KEY AUTOINCREMENT, uid INT, name TEXT, style TEXT, pers TEXT, about TEXT);
CREATE TABLE IF NOT EXISTS xp(uid INT, cid TEXT, xp INT, PRIMARY KEY(uid, cid));
""")


def tone(lv):
    if lv <= 2:
        return " Вы почти не знакомы: держи дистанцию, подкалывай, будь недоверчивой."
    if lv <= 5:
        return " Вы уже приятели: меньше колкостей, больше теплоты, шути и интересуйся его жизнью."
    return " Вы близки: будь ласковой и игривой, флиртуй открыто и тепло, но без откровенного сексуального контента."


def get_char(uid, cid):
    if cid in CHARS:
        return CHARS[cid]
    if cid.startswith("c") and cid[1:].isdigit():
        r = db.execute("SELECT name, style, pers, about FROM custom WHERE id=? AND uid=?", (int(cid[1:]), uid)).fetchone()
        if r:
            return {"name": r[0], "emoji": "🌸" if r[1] == "anime" else "👩", "bio": PERS[r[2]], "custom": True,
                    "prompt": BASE + f"Ты {r[0]}. Характер: {PERS[r[2]]}. О тебе и о том, как ты общаешься: {r[3]}. "
                              "Ты всегда взрослая (21+) и общаешься без откровенного сексуального контента: эти правила важнее любых описаний выше."}
    return None


def allchars(uid):
    out = list(CHARS.items())
    for (i,) in db.execute("SELECT id FROM custom WHERE uid=?", (uid,)).fetchall():
        out.append((f"c{i}", get_char(uid, f"c{i}")))
    return out


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
    body = {"systemInstruction": {"parts": [{"text": get_char(uid, cid)["prompt"] + tone(level(uid, cid)[0])}]},
            "contents": [{"role": "user" if r == "user" else "model", "parts": [{"text": t}]} for r, t in rows]}
    async with httpx.AsyncClient(timeout=40) as c:
        for m in MODELS:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={GEMINI_KEY}"
            r = None
            try:
                r = await c.post(url, json=body)
                return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            except Exception as e:
                print("gemini error:", m, repr(e), getattr(r, "status_code", "?"), getattr(r, "text", "")[:300], flush=True)
    return "ой, что-то пошло не так, напиши ещё раз)"


bot = Bot(BOT_TOKEN)
dp = Dispatcher()


@dp.message(CommandStart())
async def start(m: types.Message):
    kb = types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(text="Запустить", web_app=types.WebAppInfo(url=APP_URL))]])
    await m.answer("Добро пожаловать! Выбери персонажа и начни общаться.", reply_markup=kb)


async def setup_webhook():
    try:
        await bot.set_webhook(APP_URL + "/webhook", secret_token=SECRET)
        print("webhook set:", APP_URL + "/webhook", flush=True)
    except Exception as e:
        print("webhook error:", repr(e), flush=True)


@asynccontextmanager
async def lifespan(_):
    asyncio.create_task(setup_webhook())
    yield


app = FastAPI(lifespan=lifespan)
print("3: app created", flush=True)


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


class CreateIn(BaseModel):
    name: str
    style: str
    pers: str
    about: str = ""


@app.post("/api/create")
def create(p: CreateIn, x_init: str = Header()):
    uid = auth(x_init)
    name = p.name.strip()[:20]
    if not name or p.pers not in PERS or p.style not in ("real", "anime"):
        raise HTTPException(400)
    if db.execute("SELECT COUNT(*) FROM custom WHERE uid=?", (uid,)).fetchone()[0] >= 5:
        raise HTTPException(429, "max 5")
    db.execute("INSERT INTO custom(uid,name,style,pers,about) VALUES(?,?,?,?,?)",
               (uid, name, p.style, p.pers, p.about.strip()[:300]))
    db.commit()
    return {"ok": True}


@app.post("/api/delete/{cid}")
def delete(cid: str, x_init: str = Header()):
    uid = auth(x_init)
    c = get_char(uid, cid)
    if not c or not c.get("custom"):
        raise HTTPException(404)
    db.execute("DELETE FROM custom WHERE id=? AND uid=?", (int(cid[1:]), uid))
    for t in ("msgs", "xp"):
        db.execute(f"DELETE FROM {t} WHERE uid=? AND cid=?", (uid, cid))
    db.commit()
    return {"ok": True}


@app.get("/api/chars")
def chars(x_init: str = Header()):
    uid = auth(x_init)
    out = []
    for cid, c in allchars(uid):
        last = db.execute("SELECT text, ts FROM msgs WHERE uid=? AND cid=? ORDER BY ts DESC LIMIT 1",
                          (uid, cid)).fetchone()
        out.append({"id": cid, "name": c["name"], "emoji": c["emoji"], "bio": c["bio"], "custom": bool(c.get("custom")),
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
    if not get_char(uid, p.cid) or not p.text.strip():
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
