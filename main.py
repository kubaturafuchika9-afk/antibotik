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

genai.configure(api_key=GOOGLE_KEY)

ACTIVE_MODEL = None
ACTIVE_MODEL_NAME = "Searching..."

generation_config = {
  "temperature": 0.7,
  "top_p": 0.95,
  "top_k": 40,
  "max_output_tokens": 8192,
}

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
dp = Dispatcher()
app = FastAPI()

logging.basicConfig(level=logging.INFO, stream=sys.stdout)

# --- ЛОГИКА ПРИОРИТЕТОВ (ИСПРАВЛЕНА) ---

def get_dynamic_model_list():
    print("📡 Запрашиваю список моделей...")
    available_models = []
    try:
        for m in genai.list_models():
            if 'generateContent' in m.supported_generation_methods:
                name = m.name.replace("models/", "")
                if "gemini" in name:
                    available_models.append(name)
    except Exception as e:
        print(f"⚠️ Ошибка списка: {e}")
    
    # Добавляем скрытые, которые хотим проверить принудительно
    # gemini-1.5-flash-8b - это новая легкая модель, часто с хорошим лимитом
    hardcoded = ["gemini-exp-1206", "gemini-1.5-flash", "gemini-1.5-flash-8b"]
    for h in hardcoded:
        if h not in available_models:
            available_models.append(h)
            
    return list(set(available_models))

def sort_models_priority(models):
    """
    Здесь мы задаем 'вкусность' модели.
    Чем больше баллов, тем раньше бот её попробует.
    """
    def score(name):
        s = 0
        # 1. САМЫЙ ТОП: Экспериментальные (обычно безлимит для тестов)
        if "exp" in name: s += 500
        
        # 2. Надежные рабочие лошадки
        if "1.5-flash" in name: s += 300
        
        # 3. Новая супер-легкая (должна быть дешевой)
        if "8b" in name: s += 250
        
        # 4. Lite версии (как мы выяснили, 2.0-lite может быть с подвохом, поэтому ниже)
        if "lite" in name: s += 100
        
        # ШТРАФЫ
        if "pro" in name: s -= 50        # Pro быстро кончается
        if "preview" in name: s -= 20    # Preview часто имеют лимит 20/день (как ты заметил)
        
        return s

    return sorted(models, key=score, reverse=True)

async def find_best_working_model():
    global ACTIVE_MODEL, ACTIVE_MODEL_NAME
    
    candidates = get_dynamic_model_list()
    candidates = sort_models_priority(candidates)
    
    print(f"📋 Очередь проверки: {candidates}")
    
    for model_name in candidates:
        print(f"👉 Тест: {model_name}...", end=" ")
        try:
            test_model = genai.GenerativeModel(
                model_name=model_name,
                generation_config=generation_config
            )
            # Проверяем "пинг"
            response = await test_model.generate_content_async("ping")
            
            if response and response.text:
                print("✅ ЖИВАЯ! Подключаюсь.")
                ACTIVE_MODEL = test_model
                ACTIVE_MODEL_NAME = model_name
                
                # Устанавливаем системную инструкцию уже для рабочей модели
                ACTIVE_MODEL = genai.GenerativeModel(
                    model_name=model_name,
                    generation_config=generation_config,
                    system_instruction="Ты полезный помощник в Telegram. Ты умеешь слушать голосовые и смотреть фото. Отвечай кратко, емко и с юмором."
                )
                return True
                
        except Exception as e:
            err = str(e)
            if "429" in err: print("❌ (429 Лимит)")
            elif "404" in err: print("❌ (404 Нет доступа)")
            else: print(f"❌ ({err})")
    
    print("💀 Все модели недоступны.")
    return False

# --- ОБРАБОТЧИКИ ---

@dp.message(CommandStart())
async def command_start_handler(message: Message):
    status = f"✅ Модель: `{ACTIVE_MODEL_NAME}`" if ACTIVE_MODEL else "💀 Нет связи с AI"
    # Добавляем пояснение про лимиты
    if "exp" in str(ACTIVE_MODEL_NAME):
        status += "\n(Экспериментальная версия - лимиты должны быть ок)"
    elif "preview" in str(ACTIVE_MODEL_NAME):
        status += "\n⚠️ Внимание: Preview версия, возможен лимит 20/день."
        
    await message.answer(f"🤖 **Bot Reloaded**\n{status}")

@dp.message()
async def main_handler(message: Message):
    # Lazy loading: если при старте не вышло, пробуем сейчас
    if not ACTIVE_MODEL:
        await message.answer("🔄 Ищу живую модель...")
        if not await find_best_working_model():
            await message.answer("❌ Безуспешно. Google отклонил все варианты.")
            return

    bot_user = await bot.get_me()
    if not await is_addressed_to_bot(message, bot_user):
        return

    await bot.send_chat_action(chat_id=message.chat.id, action="typing")

    prompt_parts = [] 
    temp_files_to_delete = []

    try:
        text_content = ""
        if message.text:
            text_content = message.text.replace(f"@{bot_user.username}", "").strip()
        elif message.caption:
            text_content = message.caption.replace(f"@{bot_user.username}", "").strip()
        
        if text_content:
            prompt_parts.append(text_content)

        if message.photo:
            photo_id = message.photo[-1].file_id
            file_info = await bot.get_file(photo_id)
            img_data = BytesIO()
            await bot.download_file(file_info.file_path, img_data)
            img_data.seek(0)
            image = Image.open(img_data)
            prompt_parts.append(image)

        if message.voice:
            file_id = message.voice.file_id
            file_info = await bot.get_file(file_id)
            with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as temp_audio:
                await bot.download_file(file_info.file_path, destination=temp_audio.name)
                temp_path = temp_audio.name
            temp_files_to_delete.append(temp_path)

            uploaded_file = genai.upload_file(path=temp_path, mime_type="audio/ogg")
            while uploaded_file.state.name == "PROCESSING":
                await asyncio.sleep(1)
                uploaded_file = genai.get_file(uploaded_file.name)

            prompt_parts.append(uploaded_file)
            prompt_parts.append("Послушай и ответь.")

        if not prompt_parts:
            await message.reply("Пусто.")
            return

        response = await ACTIVE_MODEL.generate_content_async(prompt_parts)
        
        if response.text:
            await message.reply(response.text)
        else:
            await message.reply("...")

    except Exception as e:
        logging.error(f"Gen Error: {e}")
        # Авто-смена модели при ошибке
        if "429" in str(e) or "404" in str(e):
             await message.reply(f"⚠️ Модель {ACTIVE_MODEL_NAME} кончилась. Ищу другую...")
             if await find_best_working_model():
                 # Рекурсивный повтор (один раз) можно добавить, но пока просто уведомляем
                 await message.reply(f"✅ Перешел на {ACTIVE_MODEL_NAME}. Повтори сообщение.")
             else:
                 await message.reply("💀 Больше рабочих моделей нет.")
        else:
             await message.reply("Ошибка обработки.")
    
    finally:
        for f_path in temp_files_to_delete:
            try:
                os.remove(f_path)
            except:
                pass

# --- SERVER ---

@app.get("/")
async def root():
    return {"status": "Alive", "model": ACTIVE_MODEL_NAME}

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
