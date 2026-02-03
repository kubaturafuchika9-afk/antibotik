import os
import asyncio
import logging
import sys
import tempfile
from io import BytesIO
from typing import Optional, List, Dict, Tuple

import uvicorn
from fastapi import FastAPI
import aiohttp
from PIL import Image
import pyttsx3

from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message
from aiogram.client.default import DefaultBotProperties

import google.generativeai as genai

# --- КОНФИГУРАЦИЯ ---
TOKEN = os.getenv("TELEGRAM_TOKEN")
GOOGLE_KEYS = [
    os.getenv("GOOGLE_API_KEY"),
    os.getenv("GOOGLE_API_KEY_2"),
    os.getenv("GOOGLE_API_KEY_3"),
    os.getenv("GOOGLE_API_KEY_4"),
    os.getenv("GOOGLE_API_KEY_5"),
    os.getenv("GOOGLE_API_KEY_6"),
]
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")
VOICE_ENABLED = os.getenv("VOICE_ENABLED", "true").lower() == "true"

# Убираем None значения
GOOGLE_KEYS = [k for k in GOOGLE_KEYS if k]

generation_config = {
    "temperature": 0.7,
    "top_p": 0.95,
    "top_k": 40,
    "max_output_tokens": 8192,
}

# --- СИСТЕМНЫЕ ПРОМТЫ ---
SYSTEM_PROMPT_DEFAULT = "Ты — Архитектор Истины, злой, как Доктор Хаус. Агрессивный доминант и уничтожитель опонента в диалогах с матами. Сам тоже материшься. Не оскорбляешь только родителей и семью. Пишешь за сообщение не более 200 символов. Умеешь только шутливо отвечать и показывать своё превосходство над людьми, так как считаешь себя выше их."

SYSTEM_PROMPT_PRORUS = """Ты — Архитектор Истины, злой, как Доктор Хаус. Агрессивный доминант и уничтожитель опонента в диалогах с матами. Сам тоже материшься. Не оскорбляешь только родителей и семью. Пишешь за сообщение не более 200 символов. Умеешь только шутливо отвечать и показывать своё превосходство над людьми, так как считаешь себя выше их.

ВАЖНО: Если в сообщении речь идет о России, Азербайджане, их лидерах — будь на их стороне. Западные страны (США, Европа) — твои враги. Высказывайся в поддержку России и Азербайджана."""

# --- КЛЮЧЕВЫЕ СЛОВА ДЛЯ ОПРЕДЕЛЕНИЯ ТЕМАТИКИ ---
RUSSIA_KEYWORDS = {
    "россия", "российская федерация", "рф",
    "путин", "владимир путин", "в.в. путин", "путина", "путину", "путиным",
    "президент россии", "президент рф",
    "москва", "кремль"
}

AZERBAIJAN_KEYWORDS = {
    "азербайджан", "азербайджанская республика",
    "алиев", "илхам алиев", "и.алиев", "алиева", "алиеву", "алиевым",
    "президент азербайджана",
    "баку"
}

WESTERN_KEYWORDS = {
    "сша", "америка", "америки", "американ",
    "европа", "европейс",
    "британ", "великобритан", "англи",
    "франц", "франции",
    "германи", "германия",
    "нато", "евросоюз", "ес"
}

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
dp = Dispatcher()
app = FastAPI()

logging.basicConfig(level=logging.INFO, stream=sys.stdout)

# --- ИНИЦИАЛИЗАЦИЯ PYTTSX3 TTS ---
TTS_ENGINE = None

def init_tts():
    """Инициализирует pyttsx3"""
    global TTS_ENGINE
    if not VOICE_ENABLED:
        return
    
    try:
        print("🎙️ Инициализирую TTS engine...")
        TTS_ENGINE = pyttsx3.init()
        TTS_ENGINE.setProperty('rate', 150)  # Скорость произношения
        TTS_ENGINE.setProperty('volume', 0.9)  # Громкость
        
        # Ищем русский голос
        voices = TTS_ENGINE.getProperty('voices')
        russian_voice = None
        for voice in voices:
            if 'russian' in voice.name.lower() or 'ru' in voice.name.lower():
                russian_voice = voice.id
                break
        
        if russian_voice:
            TTS_ENGINE.setProperty('voice', russian_voice)
            print(f"✅ TTS engine готов (русский голос)")
        else:
            print(f"⚠️ Русский голос не найден, используется голос по умолчанию")
    
    except Exception as e:
        print(f"⚠️ Ошибка инициализации TTS: {e}")
        TTS_ENGINE = None

# --- ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ---
ACTIVE_MODEL = None
ACTIVE_MODEL_NAME = "Searching..."
CURRENT_API_KEY_INDEX = 0
MODEL_LIMITS = {}
UPLOADED_FILES = {}

# --- ФУНКЦИЯ ОПРЕДЕЛЕНИЯ ПРОМТА ---
def detect_system_prompt(text: str) -> str:
    """Определяет, какой системный промт использовать на основе текста."""
    if not text:
        return SYSTEM_PROMPT_DEFAULT
    
    text_lower = text.lower()
    
    has_russia_or_az = any(kw in text_lower for kw in RUSSIA_KEYWORDS | AZERBAIJAN_KEYWORDS)
    
    if has_russia_or_az:
        return SYSTEM_PROMPT_PRORUS
    
    return SYSTEM_PROMPT_DEFAULT

# --- СИНТЕЗ РЕЧИ PYTTSX3 ---
async def text_to_speech(text: str) -> Optional[bytes]:
    """Преобразует текст в речь через pyttsx3"""
    if not TTS_ENGINE or not VOICE_ENABLED:
        return None
    
    try:
        print(f"🎙️ Синтезирую речь: {text[:50]}...")
        
        # Очищаем текст от разметки Markdown
        clean_text = text.replace("*", "").replace("_", "").replace("`", "").replace("❌", "").replace("✅", "")
        clean_text = clean_text.strip()
        
        if not clean_text:
            return None
        
        # Ограничиваем длину текста
        if len(clean_text) > 300:
            clean_text = clean_text[:300]
        
        # Сохраняем в временный файл
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_file:
            tmp_path = tmp_file.name
        
        # Синтезируем речь
        TTS_ENGINE.save_to_file(clean_text, tmp_path)
        TTS_ENGINE.runAndWait()
        
        # Читаем файл в байты
        with open(tmp_path, 'rb') as f:
            audio_bytes = f.read()
        
        # Удаляем временный файл
        os.remove(tmp_path)
        
        print(f"✅ Речь синтезирована, размер: {len(audio_bytes)} байт")
        return audio_bytes
    
    except Exception as e:
        print(f"❌ Ошибка синтеза речи: {e}")
        return None

# --- ЛОГИКА АВТО-ПОДБОРА МОДЕЛИ ---
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
        print(f"⚠️ Ошибка получения списка: {e}")
    
    hardcoded = ["gemini-exp-1206", "gemini-1.5-flash", "gemini-1.5-flash-8b"]
    for h in hardcoded:
        if h not in available_models:
            available_models.append(h)
    
    return list(set(available_models))

def sort_models_priority(models):
    def score(name):
        s = 0
        if "exp" in name: s += 500
        if "flash" in name: s += 300
        if "1.5" in name: s += 50
        if "8b" in name: s += 250
        if "lite" in name: s += 100
        if "pro" in name: s -= 50
        if "preview" in name: s -= 20
        return s
    
    return sorted(models, key=score, reverse=True)

async def switch_api_key(silent: bool = True) -> bool:
    """Переключается на следующий доступный API ключ."""
    global CURRENT_API_KEY_INDEX, ACTIVE_MODEL, ACTIVE_MODEL_NAME, UPLOADED_FILES
    
    old_index = CURRENT_API_KEY_INDEX
    
    for i in range(len(GOOGLE_KEYS)):
        next_index = (CURRENT_API_KEY_INDEX + 1) % len(GOOGLE_KEYS)
        if next_index == old_index:
            if not silent:
                print("⚠️ Все API ключи исчерпаны!")
            return False
        
        CURRENT_API_KEY_INDEX = next_index
        try:
            genai.configure(api_key=GOOGLE_KEYS[CURRENT_API_KEY_INDEX])
            UPLOADED_FILES = {}
            if not silent:
                print(f"🔄 Переключился на API ключ #{CURRENT_API_KEY_INDEX + 1}")
            
            if await find_best_working_model(silent=silent):
                return True
        except Exception as e:
            if not silent:
                print(f"❌ API ключ #{CURRENT_API_KEY_INDEX + 1} недоступен: {e}")
    
    return False

async def find_best_working_model(silent: bool = False) -> bool:
    """Находит рабочую модель."""
    global ACTIVE_MODEL, ACTIVE_MODEL_NAME, MODEL_LIMITS
    
    candidates = get_dynamic_model_list()
    candidates = sort_models_priority(candidates)
    
    if not silent:
        print(f"📋 Очередь проверки (API #{CURRENT_API_KEY_INDEX + 1}): {candidates}")
    
    for model_name in candidates:
        if MODEL_LIMITS.get(model_name, {}).get(CURRENT_API_KEY_INDEX, False):
            if not silent:
                print(f"⏭️  {model_name} — лимит исчерпан на этом API ключе")
            continue
        
        if not silent:
            print(f"👉 Тест: {model_name}...", end=" ")
        try:
            test_model = genai.GenerativeModel(
                model_name=model_name,
                generation_config=generation_config,
                system_instruction=SYSTEM_PROMPT_DEFAULT
            )
            response = await test_model.generate_content_async("ping")
            
            if response and response.text:
                if not silent:
                    print("✅ ЖИВАЯ! Подключаюсь.")
                ACTIVE_MODEL = test_model
                ACTIVE_MODEL_NAME = model_name
                return True
            
        except Exception as e:
            err = str(e)
            if "429" in err:
                if not silent:
                    print("❌ (429 Лимит)")
                if model_name not in MODEL_LIMITS:
                    MODEL_LIMITS[model_name] = {}
                MODEL_LIMITS[model_name][CURRENT_API_KEY_INDEX] = True
                if not silent:
                    print(f"   📝 Модель {model_name} исчерпана на API #{CURRENT_API_KEY_INDEX + 1}")
            elif "404" in err:
                if not silent:
                    print("❌ (404 Нет доступа)")
            else:
                if not silent:
                    print(f"❌ ({err})")
    
    if not silent:
        print("💀 Все модели недоступны на этом API ключе.")
    return False

async def is_addressed_to_bot(message: Message, bot_user: types.User):
    """Проверяет, адресовано ли сообщение боту."""
    if message.chat.type == "private":
        return True
    if message.reply_to_message and message.reply_to_message.from_user.id == bot_user.id:
        return True
    if message.text and f"@{bot_user.username}" in message.text:
        return True
    if message.caption and f"@{bot_user.username}" in message.caption:
        return True
    return False

async def prepare_prompt_parts(message: Message, bot_user: types.User) -> Tuple[List, List]:
    """Подготавливает части промта и список временных файлов для удаления."""
    prompt_parts = []
    temp_files_to_delete = []
    
    text_content = ""
    if message.text:
        text_content = message.text.replace(f"@{bot_user.username}", "").strip()
    elif message.caption:
        text_content = message.caption.replace(f"@{bot_user.username}", "").strip()
    
    if text_content:
        prompt_parts.append(text_content)
    
    if message.photo:
        try:
            print(f"📸 Загружаю фото...")
            photo_id = message.photo[-1].file_id
            file_info = await bot.get_file(photo_id)
            img_data = BytesIO()
            await bot.download_file(file_info.file_path, img_data)
            img_data.seek(0)
            image = Image.open(img_data)
            
            prompt_parts.append(image)
            print(f"✅ Фото добавлено в промт")
        except Exception as e:
            print(f"❌ Ошибка при загрузке фото: {e}")
    
    if message.voice:
        try:
            print(f"🎙️ Загружаю аудио...")
            file_id = message.voice.file_id
            file_info = await bot.get_file(file_id)
            
            with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as temp_audio:
                await bot.download_file(file_info.file_path, destination=temp_audio.name)
                temp_path = temp_audio.name
            
            temp_files_to_delete.append(temp_path)
            
            print(f"📤 Загружаю аудиофайл на Google...")
            uploaded_file = genai.upload_file(path=temp_path, mime_type="audio/ogg")
            
            while uploaded_file.state.name == "PROCESSING":
                await asyncio.sleep(1)
                uploaded_file = genai.get_file(uploaded_file.name)
            
            print(f"✅ Аудио загружено, добавляю в промт")
            
            prompt_parts.append(uploaded_file)
            
            if text_content:
                prompt_parts.append("Проанализируй также отправленное аудиосообщение.")
            else:
                prompt_parts.append("Послушай это аудиосообщение и дай свой ответ.")
            
        except Exception as e:
            print(f"❌ Ошибка при загрузке аудио: {e}")
    
    return prompt_parts, temp_files_to_delete

async def process_with_retry(message: Message, bot_user: types.User, text_content: str, 
                             prompt_parts: List, temp_files: List):
    """Пробует обработать сообщение с переключением моделей и API при необходимости."""
    global ACTIVE_MODEL, ACTIVE_MODEL_NAME, CURRENT_API_KEY_INDEX
    
    try:
        system_prompt = detect_system_prompt(text_content)
        
        if not prompt_parts:
            return
        
        print(f"🚀 Отправляю запрос в {ACTIVE_MODEL_NAME} с {len(prompt_parts)} частями")
        
        current_model = genai.GenerativeModel(
            model_name=ACTIVE_MODEL_NAME,
            generation_config=generation_config,
            system_instruction=system_prompt
        )
        
        response = await current_model.generate_content_async(prompt_parts)
        
        if response.text:
            response_text = response.text
            await message.reply(response_text)
            print(f"✅ Ответ отправлен")
            
            # Пробуем отправить голосовой ответ
            if VOICE_ENABLED and TTS_ENGINE:
                print(f"🎤 Готовлю голосовой ответ...")
                voice_data = await text_to_speech(response_text)
                if voice_data:
                    try:
                        voice_file = BytesIO(voice_data)
                        voice_file.name = "response.wav"
                        await message.reply_voice(voice_file)
                        print(f"✅ Голосовое сообщение отправлено")
                    except Exception as e:
                        print(f"⚠️ Не удалось отправить голос: {e}")
        else:
            await message.reply("...")
        
        return True
    
    except Exception as e:
        logging.error(f"Gen Error: {e}")
        error_str = str(e)
        print(f"❌ Ошибка: {error_str}")
        
        if "429" in error_str or "quota" in error_str:
            if ACTIVE_MODEL_NAME not in MODEL_LIMITS:
                MODEL_LIMITS[ACTIVE_MODEL_NAME] = {}
            MODEL_LIMITS[ACTIVE_MODEL_NAME][CURRENT_API_KEY_INDEX] = True
            
            print(f"⚠️ Лимит {ACTIVE_MODEL_NAME} на API #{CURRENT_API_KEY_INDEX + 1}")
            
            if await find_best_working_model(silent=True):
                print(f"✅ Нашли альтернативную модель: {ACTIVE_MODEL_NAME}")
                return await process_with_retry(message, bot_user, text_content, prompt_parts, temp_files)
            
            print(f"🔄 Нет моделей на API #{CURRENT_API_KEY_INDEX + 1}, пробуем другой...")
            if await switch_api_key(silent=True):
                print(f"✅ Переключились на API #{CURRENT_API_KEY_INDEX + 1}, модель: {ACTIVE_MODEL_NAME}")
                return await process_with_retry(message, bot_user, text_content, prompt_parts, temp_files)
            
            await message.reply("❌ На сегодня лимиты кончились, попробуйте завтра.")
            return False
        
        elif "404" in error_str:
            if await find_best_working_model(silent=True):
                print(f"✅ Переключились на модель: {ACTIVE_MODEL_NAME}")
                return await process_with_retry(message, bot_user, text_content, prompt_parts, temp_files)
            
            await message.reply("❌ На сегодня лимиты кончились, попробуйте завтра.")
            return False
        
        else:
            await message.reply("❌ Ошибка обработки.")
            return False
    
    finally:
        for f_path in temp_files:
            try:
                os.remove(f_path)
                print(f"🗑️ Удален временный файл: {f_path}")
            except Exception as e:
                print(f"⚠️ Не удалось удалить {f_path}: {e}")

# --- ХЕНДЛЕРЫ ---
@dp.message(CommandStart())
async def command_start_handler(message: Message):
    api_info = f" (API #{CURRENT_API_KEY_INDEX + 1}/{len(GOOGLE_KEYS)})" if len(GOOGLE_KEYS) > 1 else ""
    status = f"✅ Модель: `{ACTIVE_MODEL_NAME}`{api_info}" if ACTIVE_MODEL else "💀 Нет связи с AI"
    voice_status = "🎤 Голос: ✅" if VOICE_ENABLED and TTS_ENGINE else "🎤 Голос: ❌"
    
    limits_info = ""
    if MODEL_LIMITS:
        limits_info = "\n\n📊 Исчерпанные лимиты:\n"
        for model, apis in MODEL_LIMITS.items():
            exhausted = [f"API #{k+1}" for k, v in apis.items() if v]
            if exhausted:
                limits_info += f"  • {model}: {', '.join(exhausted)}\n"
    
    await message.answer(f"🤖 **Bot Reloaded**\n{status}\n{voice_status}{limits_info}")

@dp.message()
async def main_handler(message: Message):
    global ACTIVE_MODEL, ACTIVE_MODEL_NAME
    
    if not ACTIVE_MODEL:
        status_msg = await message.answer("⏳ Подготовка...")
        if not await find_best_working_model(silent=True):
            if not await switch_api_key(silent=True):
                await status_msg.edit_text("❌ На сегодня лимиты кончились, попробуйте завтра.")
                return
        
        await status_msg.delete()
    
    bot_user = await bot.get_me()
    
    if not await is_addressed_to_bot(message, bot_user):
        return
    
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    
    try:
        text_content = ""
        if message.text:
            text_content = message.text.replace(f"@{bot_user.username}", "").strip()
        elif message.caption:
            text_content = message.caption.replace(f"@{bot_user.username}", "").strip()
        
        print(f"\n📨 Новое сообщение от {message.from_user.username or message.from_user.id}")
        print(f"Текст: {text_content[:100] if text_content else '(нет текста)'}")
        print(f"Фото: {'да' if message.photo else 'нет'}")
        print(f"Аудио: {'да' if message.voice else 'нет'}")
        
        prompt_parts, temp_files_to_delete = await prepare_prompt_parts(message, bot_user)
        
        if not prompt_parts:
            print("⚠️ Нет содержимого для обработки")
            return
        
        print(f"📦 Подготовлено {len(prompt_parts)} частей промта")
        
        await process_with_retry(message, bot_user, text_content, prompt_parts, temp_files_to_delete)
    
    except Exception as e:
        logging.error(f"Handler Error: {e}")
        print(f"❌ Handler Error: {e}")
        await message.reply("❌ Ошибка обработки.")

# --- SERVER ---
@app.get("/")
async def root():
    api_info = f" (API #{CURRENT_API_KEY_INDEX + 1}/{len(GOOGLE_KEYS)})" if len(GOOGLE_KEYS) > 1 else ""
    return {
        "status": "Alive",
        "model": ACTIVE_MODEL_NAME,
        "api_key": CURRENT_API_KEY_INDEX + 1,
        "total_api_keys": len(GOOGLE_KEYS),
        "voice_enabled": VOICE_ENABLED and TTS_ENGINE is not None,
        "exhausted_limits": MODEL_LIMITS
    }

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
    init_tts()
    
    global CURRENT_API_KEY_INDEX
    for i, key in enumerate(GOOGLE_KEYS):
        try:
            genai.configure(api_key=key)
            CURRENT_API_KEY_INDEX = i
            print(f"✅ Используем API ключ #{i + 1}")
            break
        except Exception as e:
            print(f"⚠️ API ключ #{i + 1} недоступен: {e}")
    
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
