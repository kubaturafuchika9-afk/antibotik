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

# Глобальные переменные для хранения текущей рабочей модели
ACTIVE_MODEL = None
ACTIVE_MODEL_NAME = "Searching..."

# --- НАСТРОЙКИ ГЕНЕРАЦИИ ---
generation_config = {
  "temperature": 0.7,
  "top_p": 0.95,
  "top_k": 40,
  "max_output_tokens": 8192,
}

# --- ИНИЦИАЛИЗАЦИЯ БОТА ---
bot = Bot(
    token=TOKEN, 
    default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN)
)
dp = Dispatcher()
app = FastAPI()

logging.basicConfig(level=logging.INFO, stream=sys.stdout)

# --- УМНАЯ ЛОГИКА ПОДБОРА МОДЕЛИ ---

def get_dynamic_model_list():
    """Запрашивает у Google список ВСЕХ доступных моделей для этого ключа."""
    print("📡 Запрашиваю список моделей у Google API...")
    available_models = []
    try:
        for m in genai.list_models():
            # Нам нужны только модели, которые умеют генерировать контент (чат)
            if 'generateContent' in m.supported_generation_methods:
                # Очищаем имя от приставки "models/"
                name = m.name.replace("models/", "")
                # Фильтруем мусор (только gemini)
                if "gemini" in name:
                    available_models.append(name)
    except Exception as e:
        print(f"⚠️ Ошибка получения списка: {e}")
    
    # ХАКИ: Google часто скрывает старые рабочие модели из списка. 
    # Мы добавляем их вручную, чтобы проверить их тоже.
    hardcoded_fallbacks = ["gemini-1.5-flash", "gemini-1.5-flash-latest", "gemini-1.5-pro"]
    for h in hardcoded_fallbacks:
        if h not in available_models:
            available_models.append(h)
            
    return list(set(available_models)) # Убираем дубликаты

def sort_models_priority(models):
    """Сортирует модели: сначала Lite/Flash (бесплатные), потом Pro, потом Exp."""
    def score(name):
        s = 0
        if "lite" in name: s += 100      # Lite - самый приоритет (обычно бесплатно)
        if "flash" in name: s += 50      # Flash - быстро и дешево
        if "1.5" in name: s += 20        # 1.5 - стабильнее чем 2.0
        if "exp" in name: s += 10        # Экспериментальные часто халявные
        if "pro" in name: s -= 10        # Pro - часто лимитные
        if "latest" in name: s -= 5      # Latest - непредсказуемо
        return s

    # Сортируем по убыванию "крутости" для нас
    return sorted(models, key=score, reverse=True)

async def find_best_working_model():
    """Перебирает модели и ищет живую."""
    global ACTIVE_MODEL, ACTIVE_MODEL_NAME
    
    # 1. Получаем список
    candidates = get_dynamic_model_list()
    # 2. Сортируем
    candidates = sort_models_priority(candidates)
    
    print(f"📋 Кандидаты (в порядке очереди): {candidates}")
    
    for model_name in candidates:
        print(f"👉 Тестирую: {model_name}...", end=" ")
        try:
            # Инициализация
            test_model = genai.GenerativeModel(
                model_name=model_name,
                generation_config=generation_config,
                system_instruction="Ты полезный помощник."
            )
            # Тестовый запрос (минимальный)
            response = await test_model.generate_content_async("ping")
            
            if response and response.text:
                print("✅ ЖИВАЯ! Берем.")
                ACTIVE_MODEL = test_model
                ACTIVE_MODEL_NAME = model_name
                return True
                
        except Exception as e:
            err = str(e)
            if "429" in err:
                print("❌ (429 Лимит)")
            elif "404" in err:
                print("❌ (404 Не найдена)")
            elif "400" in err:
                print(f"❌ (400 Ошибка запроса)")
            else:
                print(f"❌ (Ошибка: {err})")
    
    print("💀 ВСЕ МОДЕЛИ МЕРТВЫ. Нужен новый ключ.")
    return False

# --- ЛОГИКА БОТА ---

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
    status = f"✅ Работаю на: `{ACTIVE_MODEL_NAME}`" if ACTIVE_MODEL else "💀 Нет рабочих моделей"
    await message.answer(f"🤖 **Auto-Discovery Bot**\n{status}")

@dp.message()
async def main_handler(message: Message):
    # Если модель не найдена при старте, пробуем найти сейчас (Lazy Loading)
    if not ACTIVE_MODEL:
        await message.answer("🔄 Ищу рабочую модель, подожди...")
        if not await find_best_working_model():
            await message.answer("❌ Не нашел рабочих моделей. Проверь консоль.")
            return

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
        response = await ACTIVE_MODEL.generate_content_async(prompt_parts)
        
        if response.text:
            await message.reply(response.text)
        else:
            await message.reply("...")

    except Exception as e:
        logging.error(f"Generation Error on {ACTIVE_MODEL_NAME}: {e}")
        # Если текущая модель умерла (429), пробуем найти новую на лету
        if "429" in str(e) or "404" in str(e):
             await message.reply(f"⚠️ Модель {ACTIVE_MODEL_NAME} отвалилась. Ищу новую...")
             if await find_best_working_model():
                 await message.reply(f"✅ Переключился на {ACTIVE_MODEL_NAME}. Повтори запрос.")
             else:
                 await message.reply("💀 Все модели недоступны.")
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
    return {"status": "Alive", "current_model": ACTIVE_MODEL_NAME}

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
    # Запускаем поиск модели при старте
    await find_best_working_model()
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
