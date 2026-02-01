import os
import asyncio
import logging
import sys
import tempfile
from io import BytesIO

import uvicorn
from fastapi import FastAPI
import aiohttp
from PIL import Image

from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message
from aiogram.client.default import DefaultBotProperties

import google.generativeai as genai

# --- КОНФИГУРАЦИЯ ---
TOKEN = os.getenv("TELEGRAM_TOKEN")
GOOGLE_KEY = os.getenv("GOOGLE_API_KEY")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")

# Настройка Google Gemini
genai.configure(api_key=GOOGLE_KEY)

# --- ЖЕСТКАЯ ПРИВЯЗКА К 1.5 FLASH ---
# Это единственная модель с лимитом 1500 запросов/день.
# Мы используем её, даже если list_models() её не показывает.
MODEL_ID = "gemini-1.5-flash"

generation_config = {
  "temperature": 0.7,
  "top_p": 0.95,
  "top_k": 40,
  "max_output_tokens": 8192,
}

model = genai.GenerativeModel(
  model_name=MODEL_ID,
  generation_config=generation_config,
  system_instruction="Ты полезный помощник в Telegram. Ты умеешь слушать голосовые и смотреть фото. Отвечай кратко, емко и с юмором."
)

# --- ИНИЦИАЛИЗАЦИЯ БОТА ---
bot = Bot(
    token=TOKEN, 
    default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN)
)
dp = Dispatcher()
app = FastAPI()

logging.basicConfig(level=logging.INFO, stream=sys.stdout)

# --- ЛОГИКА ---

async def is_addressed_to_bot(message: Message, bot_user: types.User):
    if message.chat.type == "private":
        return True
    if message.reply_to_message and message.reply_to_message.from_user.id == bot_user.id:
        return True
    if message.text and f"@{bot_user.username}" in message.text:
        return True
    if message.caption and f"@{bot_user.username}" in message.caption:
        return True
    return False

# --- ХЕНДЛЕРЫ ---

@dp.message(CommandStart())
async def command_start_handler(message: Message):
    await message.answer(f"♻️ Попытка запуска на классике: `{MODEL_ID}`")

@dp.message()
async def main_handler(message: Message):
    bot_user = await bot.get_me()
    
    if not await is_addressed_to_bot(message, bot_user):
        return

    await bot.send_chat_action(chat_id=message.chat.id, action="typing")

    prompt_parts = [] 
    temp_files_to_delete = []

    try:
        # ТЕКСТ
        text_content = ""
        if message.text:
            text_content = message.text.replace(f"@{bot_user.username}", "").strip()
        elif message.caption:
            text_content = message.caption.replace(f"@{bot_user.username}", "").strip()
        
        if text_content:
            prompt_parts.append(text_content)

        # ФОТО
        if message.photo:
            photo_id = message.photo[-1].file_id
            file_info = await bot.get_file(photo_id)
            img_data = BytesIO()
            await bot.download_file(file_info.file_path, img_data)
            img_data.seek(0)
            image = Image.open(img_data)
            prompt_parts.append(image)

        # ГОЛОСОВОЕ
        if message.voice:
            file_id = message.voice.file_id
            file_info = await bot.get_file(file_id)
            with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as temp_audio:
                await bot.download_file(file_info.file_path, destination=temp_audio.name)
                temp_path = temp_audio.name
            temp_files_to_delete.append(temp_path)

            uploaded_file = genai.upload_file(path=temp_path, mime_type="audio/ogg")
            # Ждем обработки
            while uploaded_file.state.name == "PROCESSING":
                await asyncio.sleep(1)
                uploaded_file = genai.get_file(uploaded_file.name)

            prompt_parts.append(uploaded_file)
            prompt_parts.append("Послушай аудио и ответь.")

        if not prompt_parts:
            await message.reply("Пусто.")
            return

        # ГЕНЕРАЦИЯ
        response = await model.generate_content_async(prompt_parts)
        
        if response.text:
            await message.reply(response.text)
        else:
            await message.reply("...")

    except Exception as e:
        logging.error(f"Error: {e}")
        err_msg = str(e)
        if "404" in err_msg:
             await message.reply(f"❌ Модель {MODEL_ID} недоступна для этого ключа. Нужен новый аккаунт.")
        elif "429" in err_msg:
             await message.reply(f"💀 Лимит исчерпан даже на {MODEL_ID}.")
        else:
             await message.reply(f"Ошибка: {e}")
    
    finally:
        for f_path in temp_files_to_delete:
            try:
                os.remove(f_path)
            except:
                pass

# --- SERVER ---

@app.get("/")
async def root():
    return {"status": "Alive", "model": MODEL_ID}

@app.get("/health")
async def health_check():
    return {"status": "ok"}

async def keep_alive_ping():
    if not RENDER_URL:
        return
    while True:
        await asyncio.sleep(300)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{RENDER_URL}/health") as resp:
                    logging.info(f"Ping: {resp.status}")
        except Exception:
            pass

async def start_bot():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

async def start_server():
    config = uvicorn.Config(app, host="0.0.0.0", port=10000, log_level="error")
    server = uvicorn.Server(config)
    await server.serve()

async def main():
    await asyncio.gather(start_server(), start_bot(), keep_alive_ping())

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
