import base64
import hashlib
import hmac
import json
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl

import httpx
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, CommandStart
from aiogram.types import BufferedInputFile
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

BOT_TOKEN = os.environ["BOT_TOKEN"]
GEMINI_KEY = os.environ["GEMINI_KEY"]
GROQ_KEY = os.getenv("GROQ_KEY")
GROQ_MODELS = [m.strip() for m in os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").split(",") if m.strip()]
APP_URL = os.getenv("APP_URL") or os.environ["RENDER_EXTERNAL_URL"]
SECRET = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:32]
MODELS = [m.strip() for m in os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite,gemini-3.5-flash").split(",") if m.strip()]
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "gemini-2.5-flash-image")
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
        row = db.execute("SELECT name, style, pers, about FROM custom WHERE id=? AND uid=?", (int(cid[1:]), uid)).fetchone()
        if row:
            return {"name": row[0], "emoji": "🌸" if row[1] == "anime" else "👩", "bio": PERS[row[2]], "custom": True,
                    "prompt": BASE + f"Ты {row[0]}. Характер: {PERS[row[2]]}. О тебе и о том, как ты общаешься: {row[3]}. "
                              "Ты всегда взрослая (21+) и общаешься без откровенного сексуального контента."}
    return None


def allchars(uid):
    out = list(CHARS.items())
    for (i,) in db.execute("SELECT id FROM custom WHERE uid=?", (uid,)).fetchall():
        out.append((f"c{i}", get_char(uid, f"c{i}")))
    return out


def auth(init: str) -> int:
    data = dict(parse_qsl(init))
    received_hash = data.pop("hash", "")
    check = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
    key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise HTTPException(401, "bad initData")
    return json.loads(data["user"])["id"]


def left(uid: int) -> int:
    day = time.strftime("%Y-%m-%d", time.gmtime())
    row = db.execute("SELECT n FROM usage WHERE uid=? AND day=?", (uid, day)).fetchone()
    return DAILY_LIMIT - (row[0] if row else 0)


def level(uid: int, cid: str):
    row = db.execute("SELECT xp FROM xp WHERE uid=? AND cid=?", (uid, cid)).fetchone()
    xp = row[0] if row else 0
    return 1 + xp // XP_PER_LEVEL, (xp % XP_PER_LEVEL) / XP_PER_LEVEL


async def gemini(cid: str, uid: int) -> str:
    rows = db.execute("SELECT role, text FROM msgs WHERE uid=? AND cid=? ORDER BY ts DESC LIMIT 20",
                      (uid, cid)).fetchall()[::-1]
    system = get_char(uid, cid)["prompt"] + tone(level(uid, cid)[0])
    body = {"systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user" if role == "user" else "model", "parts": [{"text": text}]}
                         for role, text in rows]}

    async with httpx.AsyncClient(timeout=40) as client:
        if GROQ_KEY:
            groq_messages = [{"role": "system", "content": system}] + [
                {"role": "user" if role == "user" else "assistant", "content": text}
                for role, text in rows]
            for model in GROQ_MODELS:
                try:
                    response = await client.post(
                        "https://api.groq.com/openai/v1/chat/completions",
                        headers={"Authorization": f"Bearer {GROQ_KEY}"},
                        json={"model": model, "messages": groq_messages, "temperature": 1.0, "max_tokens": 300})
                    if response.status_code == 200:
                        return response.json()["choices"][0]["message"]["content"].strip()
                except Exception as exc:
                    print("groq error:", model, repr(exc), flush=True)

        for model in MODELS:
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_KEY}"
                response = await client.post(url, json=body)
                if response.status_code == 200:
                    return response.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            except Exception as exc:
                print("gemini error:", model, repr(exc), flush=True)
    return "ой, что-то пошло не так, напиши ещё раз)"


async def generate_photo(prompt: str) -> bytes:
    safe_prompt = ("Create a safe-for-work portrait of one fictional adult woman who is visibly 21+ and fully "
                   "clothed. Natural pose, tasteful realistic or anime style. No nudity or sexual content. "
                   f"User request: {prompt}")
    body = {"contents": [{"role": "user", "parts": [{"text": safe_prompt}]}],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{IMAGE_MODEL}:generateContent?key={GEMINI_KEY}"

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(url, json=body)
    if response.status_code != 200:
        raise RuntimeError(response.text[:500])

    for candidate in response.json().get("candidates", []):
        for part in candidate.get("content", {}).get("parts", []):
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                return base64.b64decode(inline["data"])
    raise RuntimeError("Image data was not returned")


bot = Bot(BOT_TOKEN)
dp = Dispatcher()


@dp.message(Command("photo"))
async def photo_command(message: types.Message):
    prompt = message.text.partition(" ")[2].strip()
    if not prompt:
        await message.answer("Напишите описание: /photo девушка в летнем платье у моря")
        return
    await bot.send_chat_action(message.chat.id, "upload_photo")
    try:
        image = await generate_photo(prompt)
        await message.answer_photo(BufferedInputFile(image, filename="portrait.png"), caption="Сгенерированный портрет")
    except Exception as exc:
        print("photo generation error:", repr(exc), flush=True)
        await message.answer("Не удалось сгенерировать фото. Проверьте IMAGE_MODEL и GEMINI_KEY.")


@dp.message(CommandStart())
async def start(message: types.Message):
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(text="Запустить", web_app=types.WebAppInfo(url=APP_URL))]])
    await message.answer("Добро пожаловать! Выбери персонажа и начни общаться.", reply_markup=keyboard)


@asynccontextmanager
async def lifespan(_):
    try:
        await bot.set_webhook(APP_URL + "/webhook", secret_token=SECRET)
    except Exception as exc:
        print("webhook error:", repr(exc), flush=True)
    yield


app = FastAPI(lifespan=lifespan)


class ChatIn(BaseModel):
    cid: str
    text: str


class CreateIn(BaseModel):
    name: str
    style: str
    pers: str
    about: str = ""


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


@app.post("/api/create")
def create(payload: CreateIn, x_init: str = Header()):
    uid = auth(x_init)
    name = payload.name.strip()[:20]
    if not name or payload.pers not in PERS or payload.style not in ("real", "anime"):
        raise HTTPException(400)
    if db.execute("SELECT COUNT(*) FROM custom WHERE uid=?", (uid,)).fetchone()[0] >= 5:
        raise HTTPException(429, "max 5")
    db.execute("INSERT INTO custom(uid,name,style,pers,about) VALUES(?,?,?,?,?)",
               (uid, name, payload.style, payload.pers, payload.about.strip()[:300]))
    db.commit()
    return {"ok": True}


@app.post("/api/delete/{cid}")
def delete(cid: str, x_init: str = Header()):
    uid = auth(x_init)
    character = get_char(uid, cid)
    if not character or not character.get("custom"):
        raise HTTPException(404)
    db.execute("DELETE FROM custom WHERE id=? AND uid=?", (int(cid[1:]), uid))
    for table in ("msgs", "xp"):
        db.execute(f"DELETE FROM {table} WHERE uid=? AND cid=?", (uid, cid))
    db.commit()
    return {"ok": True}


@app.get("/api/chars")
def chars(x_init: str = Header()):
    uid = auth(x_init)
    out = []
    for cid, character in allchars(uid):
        last = db.execute("SELECT text, ts FROM msgs WHERE uid=? AND cid=? ORDER BY ts DESC LIMIT 1",
                          (uid, cid)).fetchone()
        out.append({"id": cid, "name": character["name"], "emoji": character["emoji"],
                    "bio": character["bio"], "custom": bool(character.get("custom")),
                    "last": last[0] if last else None, "ts": last[1] if last else 0})
    return {"chars": out, "left": left(uid)}


@app.get("/api/history/{cid}")
def history(cid: str, x_init: str = Header()):
    uid = auth(x_init)
    rows = db.execute("SELECT role, text FROM msgs WHERE uid=? AND cid=? ORDER BY ts", (uid, cid)).fetchall()
    lv, progress = level(uid, cid)
    return {"msgs": [{"role": role, "text": text} for role, text in rows],
            "level": lv, "progress": progress, "left": left(uid)}


@app.post("/api/chat")
async def chat(payload: ChatIn, x_init: str = Header()):
    uid = auth(x_init)
    if not get_char(uid, payload.cid) or not payload.text.strip():
        raise HTTPException(400)
    if left(uid) <= 0:
        raise HTTPException(429, "limit")

    day = time.strftime("%Y-%m-%d", time.gmtime())
    db.execute("INSERT INTO msgs VALUES(?,?,?,?,?)", (uid, payload.cid, "user", payload.text[:1000], time.time()))
    db.execute("INSERT INTO usage VALUES(?,?,1) ON CONFLICT(uid,day) DO UPDATE SET n=n+1", (uid, day))
    reply = await gemini(payload.cid, uid)
    db.execute("INSERT INTO msgs VALUES(?,?,?,?,?)", (uid, payload.cid, "model", reply, time.time()))
    db.execute("INSERT INTO xp VALUES(?,?,1) ON CONFLICT(uid,cid) DO UPDATE SET xp=xp+1", (uid, payload.cid))
    db.commit()
    lv, progress = level(uid, payload.cid)
    return {"reply": reply, "level": lv, "progress": progress, "left": left(uid)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
