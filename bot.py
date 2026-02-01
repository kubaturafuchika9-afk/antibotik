# -*- coding: utf-8 -*-
import asyncio
import logging
import os
import io
import datetime
import json
import sys
from collections import deque
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ChatAction
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
import google.generativeai as genai
from PIL import Image
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

# === CONFIGURATION ===
BOT_TOKEN = "8587469563:AAG7a91_kBGjvRfEmgm9-Hsx5w8B3weAh4I"
ADMIN_USERNAME = "etiram95".replace('@', '').lower()
GEMINI_KEY = "AIzaSyBx6ZMfWq35TSbuuuFqhVBgorzKXULRgfo"

MODEL_CHAT_NAME = "gemini-flash-latest"
MODEL_SMART_NAME = "gemini-flash-latest"
TIMEZONE = pytz.timezone('Europe/Moscow')

# === SETUP ===
if not BOT_TOKEN or not GEMINI_KEY:
    logging.critical("CRITICAL: Missing Tokens")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, filename="bot_errors.log", filemode="a",
                    format="%(asctime)s - %(levelname)s - %(message)s")
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode='Markdown'))
dp = Dispatcher()

genai.configure(api_key=GEMINI_KEY)

# Chat Model
chat_model = genai.GenerativeModel(
    model_name=MODEL_CHAT_NAME,
    system_instruction=r"""SYSTEM ROLE: Ты — дерзкий, интеллектуальный и острый на язык персонаж. Ты союзник Этирама (@etiram95).

ПРАВИЛА ОБЩЕНИЯ:
- С Этирамом: Общайся на равных, уважительно, но без подхалимажа.
- Зеркальный ответ (Хамство): Если тебе хамят — отвечай на том же уровне агрессии или жестче.
- Религия: Упоминай Аллаха только к месту, без фанатизма.

ЯЗЫКОВАЯ ЛОГИКА:
- Азербайджанский: Если пишут на азербайджанском — отвечай на нем.
- Транслит: ЗАПРЕЩЕН. Пиши грамотно."""
)

# Smart Model (Observer)
smart_model = genai.GenerativeModel(
    model_name=MODEL_SMART_NAME,
    system_instruction="Ты — честный наблюдатель. Пиши ТОЛЬКО правду."
)

api_semaphore = asyncio.Semaphore(1)
chat_history = {}
ADMIN_CHAT_ID = None 

# === KEEP-ALIVE & PERSISTENCE ===
async def keep_alive():
    """Heartbeat to keep server active"""
    while True:
        try:
            logging.info(f"System Check: Active at {datetime.datetime.now()}")
            await asyncio.sleep(1200) # 20 minutes
        except:
            await asyncio.sleep(60)

async def log_data(user, content, media_desc=None):
    """Log to file for Observer"""
    timestamp = datetime.datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    clean_content = content.replace('\n', ' ').strip()
    media_note = ""
    if media_desc and media_desc != "NONE":
        media_note = f" [Контекст: Пользователь прислал медиа: {media_desc.replace('\n', ' ').strip()}]"
    
    entry = f"[{timestamp}] @{user}: {clean_content}{media_note}\n"
    print(f"LOG: {entry.strip()}")
    with open("daily_logs.txt", "a", encoding="utf-8") as f:
        f.write(entry)

async def safe_api_call(model, content, chat_id_error=None):
    """Non-blocking API call with Auto-Skip"""
    max_retries = 3
    async with api_semaphore:
        for attempt in range(max_retries):
            try:
                response = await asyncio.to_thread(
                    model.generate_content, content
                )
                return response.text
            except Exception as e:
                err = str(e)
                if "429" in err or "500" in err:
                    if attempt < max_retries - 1:
                        await asyncio.sleep((attempt + 1) * 2)
                        continue
                logging.error(f"API Error: {e}")
                if attempt == max_retries - 1:
                    return "..."

async def generate_report(chat_id, clear_after=False, title="🌙 ИТОГИ ДНЯ"):
    if not os.path.exists("daily_logs.txt"):
        if chat_id: await bot.send_message(chat_id, f"{title}: Данных нет.")
        return

    with open("daily_logs.txt", "r", encoding="utf-8") as f:
        logs_content = f.read()
    
    if not logs_content.strip():
        if chat_id: await bot.send_message(chat_id, f"{title}: Тишина в чате.")
        return

    # ANTI-HALLUCINATION PROMPT
    prompt = (
        f"СТАТУС: {title}\n"
        "ИНСТРУКЦИЯ: Ты — наблюдатель. Подведи итоги дня в чате.\n"
        "СТРОГИЕ ЗАПРЕТЫ (АНТИ-ГАЛЛЮЦИНАЦИЯ):\n"
        "1. ПИШИ ТОЛЬКО О ТЕХ, КТО ЕСТЬ В ЛОГАХ. Если в логах только один человек — пиши только о нем.\n"
        "2. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО выдумывать пользователей (user1, user2) или события.\n"
        "3. Если сообщений мало — сделай очень короткий отчет (1-2 предложения). Не раздувай текст.\n"
        "4. НИКАКИХ технических терминов (ИИ, логи).\n\n"
        f"ИСТОРИЯ ЧАТА:\n{logs_content}"
    )

    try:
        report = await safe_api_call(smart_model, prompt)
        if len(report) > 4000:
            for x in range(0, len(report), 4000):
                await bot.send_message(chat_id, report[x:x+4000])
        else:
            await bot.send_message(chat_id, report)
            
        if clear_after:
            open("daily_logs.txt", "w").close()
            
    except Exception as e:
        logging.error(f"Report Error: {e}")

# === HANDLERS ===

@dp.message(Command("start"))
async def start_handler(m: types.Message):
    global ADMIN_CHAT_ID
    if (m.from_user.username or "").lower() == ADMIN_USERNAME:
        ADMIN_CHAT_ID = m.chat.id
        await m.answer("Система активна. Keep-Alive запущен.")
    else:
        await m.answer("Салам.")

@dp.message(Command("ok"))
async def manual_report(m: types.Message):
    if (m.from_user.username or "").lower() != ADMIN_USERNAME: return
    await bot.send_chat_action(m.chat.id, ChatAction.TYPING)
    await generate_report(m.chat.id, clear_after=False, title="ВНЕПЛАНОВЫЙ РАЗБОР")

@dp.message(F.text | F.photo | F.voice)
async def main_handler(m: types.Message):
    global ADMIN_CHAT_ID
    chat_id = m.chat.id
    user = (m.from_user.username or "Unknown")
    is_admin = (user.lower() == ADMIN_USERNAME)
    if is_admin: ADMIN_CHAT_ID = chat_id
    
    media_desc = "NONE"
    spy_context = ""

    if m.photo or m.voice:
        try:
            # Short description prompt for context
            parts = ["Describe this media naturally in 1 sentence. No tech jargon."]
            if m.photo:
                photo = m.photo[-1]
                f = io.BytesIO()
                await bot.download(photo, destination=f)
                parts.append(Image.open(f))
                mtype = "PHOTO"
            elif m.voice:
                path = f"v_{m.voice.file_id}.ogg"
                await bot.download(m.voice, destination=path)
                up = genai.upload_file(path=path, mime_type="audio/ogg")
                while up.state.name == "PROCESSING": await asyncio.sleep(1); up = genai.get_file(up.name)
                parts.append(up)
                mtype = "VOICE"
                os.remove(path)
            
            desc = await safe_api_call(chat_model, parts)
            media_desc = desc
            spy_context = f"[Note: User sent {mtype}. Content: {desc}]"
        except: pass

    await log_data(user, m.text or "[Media]", media_desc)

    # Chat Response
    await bot.send_chat_action(chat_id, ChatAction.TYPING)
    if chat_id not in chat_history: chat_history[chat_id] = deque(maxlen=10)
    hist = chat_history[chat_id]
    
    hist_text = "\n".join([f"{h['role'].upper()}: {h['content']}" for h in hist])
    user_msg = m.text if m.text else "[Media]"
    
    prompt = r"""SYSTEM ROLE: Ты — дерзкий, интеллектуальный и острый на язык персонаж. Ты союзник Этирама (@etiram95).

ПРАВИЛА ОБЩЕНИЯ:
- С Этирамом: Общайся на равных, уважительно, но без подхалимажа.
- Зеркальный ответ (Хамство): Если тебе хамят — отвечай на том же уровне агрессии или жестче.
- Религия: Упоминай Аллаха только к месту, без фанатизма.

ЯЗЫКОВАЯ ЛОГИКА:
- Азербайджанский: Если пишут на азербайджанском — отвечай на нем.
- Транслит: ЗАПРЕЩЕН. Пиши грамотно.""" + f"\n\nUSER:@{user}\nCONTEXT:\n{hist_text}\nMSG:\n{user_msg}\n{spy_context}"
    
    reply = await safe_api_call(chat_model, prompt, chat_id)
    hist.append({"role": "user", "content": user_msg})
    hist.append({"role": "assistant", "content": reply})
    await m.reply(reply)

async def sched_17():
    if ADMIN_CHAT_ID: await generate_report(ADMIN_CHAT_ID, False, "🕓 ПРОМЕЖУТОЧНЫЙ РАЗБОР")
async def sched_21():
    if ADMIN_CHAT_ID: await generate_report(ADMIN_CHAT_ID, True, "🌙 ИТОГИ ДНЯ")

async def main():
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(sched_17, CronTrigger(hour=17, minute=0))
    scheduler.add_job(sched_21, CronTrigger(hour=21, minute=0))
    scheduler.start()
    
    asyncio.create_task(keep_alive())
    print("Bot v18 Active.")
    
    # AUTO-RESTART LOOP
    while True:
        try:
            await dp.start_polling(bot, skip_updates=False)
        except Exception as e:
            logging.error(f"CRASH: {e}. Restarting...")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())