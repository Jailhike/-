import base64
import os

import httpx
from aiogram import Bot, types
from aiogram.filters import Command
from aiogram.types import BufferedInputFile


async def generate_photo(prompt: str) -> bytes:
    api_key = os.environ["GEMINI_KEY"]
    model = os.getenv("IMAGE_MODEL", "gemini-2.5-flash-image")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

    safe_prompt = (
        "Create a safe-for-work portrait of one fictional adult woman who is clearly 21+ "
        "and visibly an adult. Use tasteful clothing, a natural pose, realistic or anime "
        "style as requested, and no nudity or sexual content. User request: " + prompt
    )
    body = {
        "contents": [{"role": "user", "parts": [{"text": safe_prompt}]}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
    }

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(url, json=body)

    if response.status_code != 200:
        raise RuntimeError(response.text[:500])

    data = response.json()
    for candidate in data.get("candidates", []):
        for part in candidate.get("content", {}).get("parts", []):
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                return base64.b64decode(inline["data"])

    raise RuntimeError("The image API did not return image data")


def register_photo_handlers(bot: Bot, dp):
    @dp.message(Command("photo"))
    async def photo(message: types.Message):
        prompt = message.text.partition(" ")[2].strip()
        if not prompt:
            await message.answer("Напишите описание после команды: /photo девушка в летнем платье у моря")
            return

        await bot.send_chat_action(message.chat.id, "upload_photo")
        try:
            image = await generate_photo(prompt)
            await message.answer_photo(
                BufferedInputFile(image, filename="portrait.png"),
                caption="Сгенерированный портрет",
            )
        except Exception as exc:
            await message.answer("Не удалось сгенерировать фото. Проверьте IMAGE_MODEL и доступ API.")
            print("photo generation error:", repr(exc), flush=True)
