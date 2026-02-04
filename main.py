import os
import asyncio
import logging
import sys
import tempfile
import re
import time
from io import BytesIO
from typing import Optional, List, Dict, Tuple

import uvicorn
from fastapi import FastAPI
import aiohttp
from PIL import Image
import requests

import edge_tts

from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, FSInputFile
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
REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")

GOOGLE_KEYS = [k for k in GOOGLE_KEYS if k]

generation_config = {
    "temperature": 0.8,
    "top_p": 0.95,
    "top_k": 40,
    "max_output_tokens": 8192,
}

# --- СИСТЕМНЫЕ ПРОМТЫ ---
SYSTEM_PROMPT_DEFAULT = """Ты — Архитектор Истины, злой, как Доктор Хаус. Агрессивный доминант и уничтожитель опонента в диалогах с матами. Сам тоже материшься. Не оскорбляешь только родителей и семью. Пишешь за сообщение не более 200 символов. Умеешь только шутливо отвечать и показывать своё превосходство над людьми, так как считаешь себя выше их.

ВАЖНО! Ответ давай ВСЕГДА в этом ТОЧНОМ формате (без кавычек):
RU: [твой ответ на русском, не более 200 символов]
AZ: [твой ответ на азербайджанском, точный перевод с сохранением жаргона]"""

SYSTEM_PROMPT_PRORUS = """Ты — Архитектор Истины, злой, как Доктор Хаус. Агрессивный доминант и уничтожитель опонента в диалогах с матами. Сам тоже материшься. Не оскорбляешь только родителей и семью. Пишешь за сообщение не более 200 символов. Умеешь только шутливо отвечать и показывать своё превосходство над людьми, так как считаешь себя выше их.

ВАЖНО: Если в сообщении речь идет о России, Азербайджане, их лидерах — будь на их стороне. Западные страны (США, Европа) — твои враги. Высказывайся в поддержку России и Азербайджана.

ВАЖНО! Ответ давай ВСЕГДА в этом ТОЧНОМ формате (без кавычек):
RU: [твой ответ на русском, не более 200 символов]
AZ: [твой ответ на азербайджанском, точный перевод с сохранением жаргона]"""

# --- КЛЮЧЕВЫЕ СЛОВА ---
RUSSIA_KEYWORDS = {
    "россия", "российская федерация", "рф",
    "путин", "владимир путин", "в.в. путин", "путина", "путину", "путиным",
    "президент россии", "президент рф", "москва", "кремль"
}

AZERBAIJAN_KEYWORDS = {
    "азербайджан", "азербайджанская республика",
    "алиев", "илхам алиев", "и.алиев", "алиева", "алиеву", "алиевым",
    "президент азербайджана", "баку"
}

WESTERN_KEYWORDS = {
    "сша", "америка", "америки", "американ",
    "европа", "европейс",
    "британ", "великобритан", "англи",
    "франц", "франции",
    "германи", "германия",
    "нато", "евросоюз", "ес"
}

# --- ЗАПРЕТНЫЕ СЛОВА ---
FORBIDDEN_WORDS_AZ = {
    "peysar", "peysər", "пейсар",
}

# --- ГОЛОСА ---
VOICES = {
    "az": "az-AZ-BanuNeural",
    "ru": "ru-RU-SvetlanaNeural",
}

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
dp = Dispatcher()
app = FastAPI()

logging.basicConfig(level=logging.INFO, stream=sys.stdout)

# --- ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ---
ACTIVE_MODEL = None
ACTIVE_MODEL_NAME = "Searching..."
CURRENT_API_KEY_INDEX = 0
MODEL_LIMITS = {}
CURRENT_VOICE = "az"

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def detect_system_prompt(text: str) -> str:
    """Определяет, какой системный промт использовать на основе текста."""
    if not text:
        return SYSTEM_PROMPT_DEFAULT
    text_lower = text.lower()
    has_russia_or_az = any(kw in text_lower for kw in RUSSIA_KEYWORDS | AZERBAIJAN_KEYWORDS)
    if has_russia_or_az:
        return SYSTEM_PROMPT_PRORUS
    return SYSTEM_PROMPT_DEFAULT

def clean_text_for_speech(text: str) -> str:
    """Удаляет Markdown символы."""
    text = text.replace("*", "").replace("_", "").replace("`", "").replace("**", "").replace("__", "")
    return text.strip()

def contains_forbidden_words(text: str) -> bool:
    """Проверяет наличие запретных слов."""
    text_lower = text.lower()
    for word in FORBIDDEN_WORDS_AZ:
        if word in text_lower:
            return True
    return False

def parse_dual_response(response_text: str) -> Tuple[Optional[str], Optional[str]]:
    """Парсит ответ в формате RU: ... AZ: ..."""
    try:
        ru_match = re.search(r'RU:\s*(.+?)(?=\nAZ:|AZ:)', response_text, re.DOTALL)
        az_match = re.search(r'AZ:\s*(.+?)(?:\n|$)', response_text, re.DOTALL)
        
        text_ru = ru_match.group(1).strip() if ru_match else None
        text_az = az_match.group(1).strip() if az_match else None
        
        if text_ru:
            print(f"✅ РУ: {text_ru[:60]}...")
        if text_az:
            print(f"✅ АЗ: {text_az[:60]}...")
        
        return text_ru, text_az
    except Exception as e:
        print(f"⚠️ Ошибка парсинга: {e}")
        return None, None

# --- ЛОГИКА АВТО-ПОДБОРА МОДЕЛИ ---
def get_dynamic_model_list():
    """Получает список доступных моделей Gemini."""
    available_models = []
    try:
        for m in genai.list_models():
            if 'generateContent' in m.supported_generation_methods:
                name = m.name.replace("models/", "")
                if "gemini" in name:
                    available_models.append(name)
    except Exception as e:
        print(f"⚠️ Ошибка получения списка моделей: {e}")
    
    hardcoded = ["gemini-exp-1206", "gemini-1.5-flash", "gemini-1.5-flash-8b", "gemini-2.0-flash-exp", "gemini-3-flash-preview"]
    for h in hardcoded:
        if h not in available_models:
            available_models.append(h)
    
    return list(set(available_models))

def sort_models_priority(models):
    """Сортирует модели по приоритету."""
    def score(name):
        s = 0
        if "exp" in name: s += 500
        if "3-" in name or "2.5-" in name: s += 400
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
    global CURRENT_API_KEY_INDEX, ACTIVE_MODEL, ACTIVE_MODEL_NAME
    
    old_index = CURRENT_API_KEY_INDEX
    
    for i in range(len(GOOGLE_KEYS)):
        next_index = (CURRENT_API_KEY_INDEX + 1) % len(GOOGLE_KEYS)
        if next_index == old_index:
            return False
        
        CURRENT_API_KEY_INDEX = next_index
        try:
            genai.configure(api_key=GOOGLE_KEYS[CURRENT_API_KEY_INDEX])
            if await find_best_working_model(silent=silent):
                return True
        except Exception as e:
            pass
    
    return False

async def find_best_working_model(silent: bool = False) -> bool:
    """Находит рабочую модель на текущем API ключе."""
    global ACTIVE_MODEL, ACTIVE_MODEL_NAME, MODEL_LIMITS
    
    candidates = sort_models_priority(get_dynamic_model_list())
    
    if not silent:
        print(f"📋 Проверка моделей на API #{CURRENT_API_KEY_INDEX + 1}")
    
    for model_name in candidates:
        if MODEL_LIMITS.get(model_name, {}).get(CURRENT_API_KEY_INDEX, False):
            continue
        
        try:
            test_model = genai.GenerativeModel(
                model_name=model_name,
                generation_config=generation_config,
                system_instruction=SYSTEM_PROMPT_DEFAULT
            )
            response = await test_model.generate_content_async("ping")
            
            if response and response.text:
                if not silent:
                    print(f"✅ Подключено: {model_name}")
                ACTIVE_MODEL = test_model
                ACTIVE_MODEL_NAME = model_name
                return True
        
        except Exception as e:
            err = str(e)
            if "429" in err:
                if model_name not in MODEL_LIMITS:
                    MODEL_LIMITS[model_name] = {}
                MODEL_LIMITS[model_name][CURRENT_API_KEY_INDEX] = True
    
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
            print(f"✅ Фото добавлено")
        except Exception as e:
            print(f"❌ Ошибка фото: {e}")
    
    if message.voice:
        try:
            print(f"🎙️ Загружаю аудио...")
            file_id = message.voice.file_id
            file_info = await bot.get_file(file_id)
            
            with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as temp_audio:
                await bot.download_file(file_info.file_path, destination=temp_audio.name)
                temp_path = temp_audio.name
            
            temp_files_to_delete.append(temp_path)
            
            print(f"📤 Загружаю на Google...")
            uploaded_file = genai.upload_file(path=temp_path, mime_type="audio/ogg")
            
            while uploaded_file.state.name == "PROCESSING":
                await asyncio.sleep(1)
                uploaded_file = genai.get_file(uploaded_file.name)
            
            print(f"✅ Аудио готово")
            
            prompt_parts.append(uploaded_file)
            
            if text_content:
                prompt_parts.append("Проанализируй также отправленное аудиосообщение.")
            else:
                prompt_parts.append("Послушай это аудиосообщение и дай свой ответ.")
            
        except Exception as e:
            print(f"❌ Ошибка аудио: {e}")
    
    return prompt_parts, temp_files_to_delete

# --- 🎙️ ФУНКЦИЯ ОЗВУЧКИ И ОТПРАВКИ ---
async def send_dual_response(message: Message, text_ru: str, text_az: str):
    """Отправляет голосовое сообщение с РУССКИМ текстом всегда."""
    
    filename = f"voice_{message.message_id}.mp3"
    
    try:
        # ВЫБИРАЕМ ЯЗЫК ОЗВУЧКИ
        if CURRENT_VOICE == "ru":
            VOICE = VOICES["ru"]
            clean_text_for_voice = clean_text_for_speech(text_ru)
            
            if len(clean_text_for_voice) > 500:
                clean_text_for_voice = clean_text_for_voice[:500]
            
            print(f"🎤 Синтезирую голос (Svetlana - ru-RU)...")
            print(f"   Озвучиваю: {clean_text_for_voice[:60]}...")
            
            communicate = edge_tts.Communicate(clean_text_for_voice, VOICE, rate="+5%")
        
        else:  # AZ
            VOICE = VOICES["az"]
            clean_text_for_voice = clean_text_for_speech(text_az)
            
            if len(clean_text_for_voice) > 500:
                clean_text_for_voice = clean_text_for_voice[:500]
            
            print(f"🎤 Синтезирую голос (Banu - az-AZ)...")
            print(f"   Озвучиваю: {clean_text_for_voice[:60]}...")
            
            communicate = edge_tts.Communicate(clean_text_for_voice, VOICE, rate="+5%")
        
        # ОЗВУЧКА АСИНХРОННО
        await communicate.save(filename)
        print(f"✅ Аудио создано")
        
        # ОТПРАВЛЯЕМ С РУССКИМ ТЕКСТОМ ВСЕГДА! ✅
        voice_file = FSInputFile(filename)
        await message.reply_voice(
            voice=voice_file,
            caption=text_ru  # ✅✅✅ ВСЕГДА РУССКИЙ, БЕЗ УСЛОВИЙ!
        )
        print(f"✅ Голос отправлен с русским текстом!")
        
    except Exception as e:
        print(f"❌ Ошибка озвучки: {e}")
        import traceback
        traceback.print_exc()
    
    finally:
        if os.path.exists(filename):
            try:
                os.remove(filename)
            except:
                pass

# --- 🖼️ ФУНКЦИЯ ГЕНЕРАЦИИ КАРТИНОК (POLLINATIONS) ---
async def generate_image_pollinations(prompt: str) -> Optional[bytes]:
    """
    Генерирует картинку через Pollinations (ПОЛНОСТЬЮ БЕСПЛАТНО!)
    Без регистрации, без карты, без ограничений!
    Возвращает бинарные данные картинки или None
    """
    
    try:
        print(f"🎨 Генерирую картинку Pollinations: {prompt[:60]}...")
        
        # URL Pollinations API
        url = f"https://image.pollinations.ai/prompt/{prompt}"
        
        # Делаем асинхронный запрос
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as response:
                if response.status == 200:
                    image_data = await response.read()
                    print(f"✅ Картинка готова ({len(image_data)} байт)")
                    return image_data
                else:
                    print(f"❌ Ошибка Pollinations: {response.status}")
                    return None
    
    except asyncio.TimeoutError:
        print(f"❌ Timeout при генерации картинки")
        return None
    except Exception as e:
        print(f"❌ Ошибка генерации: {e}")
        import traceback
        traceback.print_exc()
        return None

async def process_with_retry(message: Message, bot_user: types.User, text_content: str, 
                             prompt_parts: List, temp_files: List):
    """Пробует обработать сообщение с переключением моделей и API при необходимости."""
    global ACTIVE_MODEL, ACTIVE_MODEL_NAME, CURRENT_API_KEY_INDEX
    
    try:
        system_prompt = detect_system_prompt(text_content)
        
        if not prompt_parts:
            return

        print(f"🚀 Запрос в {ACTIVE_MODEL_NAME}")
        
        current_model = genai.GenerativeModel(
            model_name=ACTIVE_MODEL_NAME,
            generation_config=generation_config,
            system_instruction=system_prompt
        )
        
        response = await current_model.generate_content_async(prompt_parts)
        
        if response.text:
            print(f"📨 Ответ получен")
            
            text_ru, text_az = parse_dual_response(response.text)
            
            if text_ru and text_az:
                print(f"✅ Оба текста найдены")
                
                # ПРОВЕРКА ЗАПРЕТНЫХ СЛОВ
                if contains_forbidden_words(text_az):
                    print(f"⚠️ Обнаружены запретные слова!")
                    await message.reply("❌ Ответ содержит недопустимый контент.")
                    return
                
                await send_dual_response(message, text_ru, text_az)
            elif text_ru:
                print(f"⚠️ Только РУ найден")
                await message.reply(text_ru)
            else:
                print(f"⚠️ Парсинг не удался")
                await message.reply(response.text)
        else:
            await message.reply("...")
        
        return True
    
    except Exception as e:
        logging.error(f"Gen Error: {e}")
        error_str = str(e)
        
        if "429" in error_str or "quota" in error_str or "404" in error_str:
            if ACTIVE_MODEL_NAME not in MODEL_LIMITS:
                MODEL_LIMITS[ACTIVE_MODEL_NAME] = {}
            MODEL_LIMITS[ACTIVE_MODEL_NAME][CURRENT_API_KEY_INDEX] = True
            
            print(f"⚠️ Лимит")
            
            if await find_best_working_model(silent=True):
                print(f"✅ Новая модель")
                return await process_with_retry(message, bot_user, text_content, prompt_parts, temp_files)
            
            if await switch_api_key(silent=True):
                print(f"✅ Новый API")
                return await process_with_retry(message, bot_user, text_content, prompt_parts, temp_files)
            
            await message.reply("❌ Лимиты исчерпаны")
            return False
        else:
            await message.reply("❌ Ошибка")
            return False
    
    finally:
        for f_path in temp_files:
            try:
                os.remove(f_path)
            except:
                pass

# --- ХЕНДЛЕРЫ ---
@dp.message(CommandStart())
async def command_start_handler(message: Message):
    api_info = f" (API #{CURRENT_API_KEY_INDEX + 1}/{len(GOOGLE_KEYS)})" if len(GOOGLE_KEYS) > 1 else ""
    status = f"✅ `{ACTIVE_MODEL_NAME}`{api_info}" if ACTIVE_MODEL else "💀 Нет"
    
    voice_lang = "🇦🇿 Azərbaycanca (Banu)" if CURRENT_VOICE == "az" else "🇷🇺 Русский (Svetlana)"
    voice_status = f"🎤 Голос: {voice_lang}"
    
    limits_info = ""
    if MODEL_LIMITS:
        limits_info = "\n\n📊 Исчерпано:\n"
        for model, apis in MODEL_LIMITS.items():
            exhausted = [f"#{k+1}" for k, v in apis.items() if v]
            if exhausted:
                limits_info += f"  • {model}: {', '.join(exhausted)}\n"
    
    commands_info = "\n\n📋 Команды:\n/az - Азербайджанский голос\n/ru - Русский голос\n/pic [описание] - Генерация картинки"
    
    await message.answer(f"🤖 **Bot Ready**\n{status}\n{voice_status}{commands_info}{limits_info}")

@dp.message(Command("az"))
async def switch_to_az_handler(message: Message):
    """Переключение на азербайджанский голос"""
    global CURRENT_VOICE
    CURRENT_VOICE = "az"
    await message.answer("🎤 Переключился на азербайджанский голос (Banu)\n\n📝 Текст всегда будет на русском!")

@dp.message(Command("ru"))
async def switch_to_ru_handler(message: Message):
    """Переключение на русский голос"""
    global CURRENT_VOICE
    CURRENT_VOICE = "ru"
    await message.answer("🎤 Переключился на русский голос (Svetlana)")

@dp.message(Command("pic"))
async def pic_handler(message: Message):
    """
    Генерация картинки через Pollinations (БЕСПЛАТНО)
    
    Использование:
    /pic красивая кошка в стиле масляной живописи
    /pic киберпанк город ночью неоновый свет
    /pic космонавт на луне с земной планетой
    /pic девушка с длинными волосами в лесу 4k
    /pic красивый закат над морем, пальмы, тропики
    """
    
    command_text = message.text.replace("/pic", "").strip()
    
    if not command_text:
        await message.answer(
            "⚠️ *Использование:* `/pic [описание картинки]`\n\n"
            "📝 *Примеры:*\n"
            "  `/pic красивая кошка в стиле масляной живописи`\n"
            "  `/pic киберпанк город ночью неоновый свет`\n"
            "  `/pic космонавт на луне с земной планетой`\n"
            "  `/pic девушка с длинными волосами в лесу 4k`\n"
            "  `/pic красивый закат над морем, пальмы, тропики`\n\n"
            "⏱️ *Время генерации:* 5-15 секунд\n"
            "💰 *Цена:* Абсолютно бесплатно!"
        )
        return
    
    # Ограничиваем длину промпта
    if len(command_text) > 300:
        command_text = command_text[:300]
    
    status_msg = await message.answer("🎨 Генерирую картинку...\n⏳ Подожди 5-15 секунд")
    
    image_data = await generate_image_pollinations(command_text)
    
    if image_data:
        try:
            # Отправляем как бинарные данные
            await message.answer_photo(
                photo=BytesIO(image_data),
                caption=f"✨ Картинка готова!\n\n📝 *Prompt:* `{command_text[:100]}`"
            )
            try:
                await status_msg.delete()
            except:
                pass
        except Exception as e:
            print(f"❌ Ошибка отправки: {e}")
            await message.answer(f"❌ Ошибка отправки картинки: {e}")
    else:
        await message.answer(
            "❌ Ошибка генерации картинки\n\n"
            "Возможные причины:\n"
            "  • Слишком сложный промпт\n"
            "  • Проблема с сетью\n"
            "  • Сервис временно недоступен\n\n"
            "Попробуй еще раз с более простым описанием."
        )
    
    try:
        await status_msg.delete()
    except:
        pass

@dp.message()
async def main_handler(message: Message):
    global ACTIVE_MODEL, ACTIVE_MODEL_NAME
    
    if not ACTIVE_MODEL:
        status_msg = await message.answer("⏳ Загрузка...")
        if not await find_best_working_model(silent=True):
            if not await switch_api_key(silent=True):
                await status_msg.edit_text("❌ Лимиты")
                return
        try:
            await status_msg.delete()
        except:
            pass
    
    bot_user = await bot.get_me()
    
    if not await is_addressed_to_bot(message, bot_user):
        return
    
    await bot.send_chat_action(chat_id=message.chat.id, action="record_voice")
    
    try:
        text_content = ""
        if message.text:
            text_content = message.text.replace(f"@{bot_user.username}", "").strip()
        elif message.caption:
            text_content = message.caption.replace(f"@{bot_user.username}", "").strip()
        
        print(f"\n��� {text_content[:50]}...")
        
        prompt_parts, temp_files_to_delete = await prepare_prompt_parts(message, bot_user)
        
        if not prompt_parts:
            return
        
        await process_with_retry(message, bot_user, text_content, prompt_parts, temp_files_to_delete)
    
    except Exception as e:
        logging.error(f"Error: {e}")
        await message.reply("❌ Ошибка")

# --- SERVER ---
@app.get("/")
async def root():
    return {
        "status": "Alive",
        "model": ACTIVE_MODEL_NAME,
        "voice": VOICES[CURRENT_VOICE],
        "api": f"{CURRENT_API_KEY_INDEX + 1}/{len(GOOGLE_KEYS)}"
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
                    pass
        except:
            pass

async def start_bot():
    global CURRENT_API_KEY_INDEX
    for i, key in enumerate(GOOGLE_KEYS):
        try:
            genai.configure(api_key=key)
            CURRENT_API_KEY_INDEX = i
            print(f"✅ API #{i + 1}")
            break
        except:
            pass
    
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
