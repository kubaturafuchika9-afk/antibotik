import os
import asyncio
import logging
import uvicorn
from fastapi import FastAPI
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.enums import ChatType
from io import BytesIO

import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PORT = int(os.getenv("PORT", 8080))

genai.configure(api_key=GEMINI_API_KEY)

# СТРОГО указанная модель
MODEL_NAME = "gemini-flash-latest"

generation_config = {
    "temperature": 0.7,
    "top_p": 0.95,
    "top_k": 64,
    "max_output_tokens": 8192,
}

# Отключаем фильтры безопасности
safety_settings = {
    HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
}

model = genai.GenerativeModel(
    model_name=MODEL_NAME,
    generation_config=generation_config,
    safety_settings=safety_settings
)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
app = FastAPI()

# --- WEB SERVER (PING PONG) ---
@app.get("/")
async def health_check():
    return {"status": "alive", "message": "Bot is running with gemini-flash-latest"}

@app.head("/")
async def head_check():
    return {"status": "alive"}

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

async def is_message_for_me(message: types.Message):
    if message.chat.type == ChatType.PRIVATE:
        return True
    
    bot_user = await bot.get_me()
    
    if message.reply_to_message and message.reply_to_message.from_user.id == bot_user.id:
        return True
    
    if message.entities:
        for entity in message.entities:
            if entity.type == "mention":
                if bot_user.username in message.text:
                    return True
    return False

async def send_to_gemini(prompt_parts):
    try:
        response = await model.generate_content_async(prompt_parts)
        return response.text
    except Exception as e:
        logging.error(f"Error Gemini: {e}")
        return f"Ошибка API: {e}"

# --- ХЕНДЛЕРЫ ---

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer("Привет! Я готов к работе.")

@dp.message()
async def handle_all_messages(message: types.Message):
    if not await is_message_for_me(message):
        return

    await bot.send_chat_action(chat_id=message.chat.id, action="typing")

    prompt_parts = []
    user_text = message.text or message.caption or ""

    # Обработка фото
    if message.photo:
        photo = message.photo[-1]
        photo_file = await bot.download(photo, destination=BytesIO())
        photo_data = photo_file.getvalue()
        
        prompt_parts.append({
            "mime_type": "image/jpeg",
            "data": photo_data
        })
        if not user_text:
            user_text = "Что на фото?"

    # Обработка голосовых
    elif message.voice:
        voice_file = await bot.get_file(message.voice.file_id)
        voice_io = await bot.download_file(voice_file.file_path, destination=BytesIO())
        voice_data = voice_io.getvalue()
        
        prompt_parts.append({
            "mime_type": "audio/ogg",
            "data": voice_data
        })
        if not user_text:
            user_text = "Ответь на это голосовое сообщение."

    if user_text:
        prompt_parts.append(user_text)

    if not prompt_parts:
        return

    try:
        response_text = await send_to_gemini(prompt_parts)
        await message.reply(response_text, parse_mode="Markdown")
    except Exception as e:
        await message.reply(f"Ошибка: {e}")

# --- ЗАПУСК ---
async def start_bot():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

async def main():
    config = uvicorn.Config(app, host="0.0.0.0", port=PORT)
    server = uvicorn.Server(config)
    await asyncio.gather(server.serve(), start_bot())

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
