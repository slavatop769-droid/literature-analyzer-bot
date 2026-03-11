import asyncio
import logging
import re
import hashlib
import json
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from openai import OpenAI
import httpx
from datetime import datetime
from flask import Flask
import threading

# Flask сервер для поддержания активности
app = Flask(__name__)

@app.route('/')
@app.route('/health')
def health():
    return "Bot is running!", 200

def run_flask():
    app.run(host='0.0.0.0', port=8080)

# Запускаем Flask в фоновом потоке
flask_thread = threading.Thread(target=run_flask, daemon=True)
flask_thread.start()

# Настройка логирования
logging.basicConfig(level=logging.INFO, 
                   format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ============================================
# ТОКЕНЫ
# ============================================

TELEGRAM_TOKEN = "8786705915:AAGP0p3RVuQF4IMKE_QbjfNicKXJNU7Qaw8"
OPENROUTER_API_KEY = "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"

# Настройки OpenRouter
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_REFERER = "https://t.me/Literatureaventt0_bot"
OPENROUTER_TITLE = "Literature Analyzer Bot"

# Настройки таймаутов
HTTP_TIMEOUT = 60.0
MAX_RETRIES = 2

# Запасные модели на случай если API не вернет список
FALLBACK_MODELS = [
    "google/gemini-2.0-flash-exp:free",
    "meta-llama/llama-3.2-3b-instruct:free",
    "microsoft/phi-3-mini-128k-instruct:free",
    "mistralai/mistral-7b-instruct:free",
    "deepseek/deepseek-chat:free"
]

# Создаем синхронный HTTP клиент для OpenAI
http_client = httpx.Client(
    timeout=httpx.Timeout(
        connect=20.0,
        read=60.0,
        write=20.0,
        pool=20.0
    ),
    limits=httpx.Limits(
        max_keepalive_connections=10,
        max_connections=20
    )
)

# Инициализация OpenAI клиента с синхронным HTTP клиентом
client = OpenAI(
    base_url=OPENROUTER_BASE_URL,
    api_key=OPENROUTER_API_KEY,
    http_client=http_client,
    default_headers={
        "HTTP-Referer": OPENROUTER_REFERER,
        "X-Title": OPENROUTER_TITLE,
    }
)

# Инициализация бота
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

# Глобальные переменные
available_models = []
current_model = None
last_query = {}
user_tasks = {}

# Счетчик запросов для статистики
total_requests = 0
character_requests = 0
work_requests = 0

# Состояния для FSM
class AnalysisStates(StatesGroup):
    waiting_for_text = State()
    waiting_for_character = State()

# ============================================
# СТАРЫЕ ПОДРОБНЫЕ ПРОМПТЫ
# ============================================

LITERARY_ANALYSIS_PROMPT = """Ты - эксперт по литературному анализу. Твоя задача - давать подробный, но структурированный анализ литературных произведений.

Для каждого запроса предоставляй:

📖 КРАТКОЕ СОДЕРЖАНИЕ (минимум 10 предложений) - очень подробный пересказ сюжета с основными событиями, развитием конфликта и развязкой.

👥 ГЛАВНЫЕ ПЕРСОНАЖИ (4-5 персонажей) - подробная характеристика каждого, их роль в произведении, эволюция характера.

🎯 ОСНОВНЫЕ ТЕМЫ (3-4 темы) - ключевые идеи и проблемы, поднятые в произведении, с примерами из текста.

💫 ИНТЕРЕСНЫЕ ФАКТЫ (3-4 факта) - любопытные детали о создании произведения, авторе, историческом контексте или скрытых смыслах.

⚠️ ВАЖНО: Ответ должен быть ПОЛНЫМ и ПОДРОБНЫМ. Не сокращай анализ. Пиши развернуто.
"""

CHARACTER_ANALYSIS_PROMPT = """Ты - эксперт по литературному анализу. Твоя задача - давать подробный анализ конкретного персонажа литературного произведения.

Произведение: {work}
Персонаж: {character}

Предоставь следующую информацию о персонаже:

📖 БИОГРАФИЯ И ПРОИСХОЖДЕНИЕ - откуда персонаж родом, его социальное положение, семья, образование.

🎭 ХАРАКТЕР И ЛИЧНОСТНЫЕ КАЧЕСТВА - подробное описание характера, сильные и слабые стороны, мотивация, внутренние конфликты.

📈 ЭВОЛЮЦИЯ ПЕРСОНАЖА - как меняется персонаж на протяжении произведения, ключевые моменты развития.

🔗 ОТНОШЕНИЯ С ДРУГИМИ ПЕРСОНАЖАМИ - важные связи и взаимодействия, влияние на других героев.

⚡ КЛЮЧЕВЫЕ СЦЕНЫ С УЧАСТИЕМ ПЕРСОНАЖА - самые важные моменты, где персонаж раскрывается наиболее ярко.

💫 ИНТЕРЕСНЫЕ ФАКТЫ - любопытные детали о персонаже, возможные прототипы, символизм.

⚠️ ВАЖНО: Ответ должен быть подробным и содержательным. Используй эмодзи для разделения секций.
"""

# ============================================
# ФУНКЦИЯ ДЛЯ ВЫВОДА В КОНСОЛЬ
# ============================================

def log_user_request(user_info, request_type, content):
    """Логирует запрос пользователя в консоль"""
    global total_requests, work_requests, character_requests
    
    total_requests += 1
    
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    user_name = user_info.full_name if user_info.full_name else user_info.username if user_info.username else f"ID:{user_info.id}"
    user_id = user_info.id
    
    if request_type == "work":
        work_requests += 1
        emoji = "📚"
        type_str = "ПРОИЗВЕДЕНИЕ"
    elif request_type == "character":
        character_requests += 1
        emoji = "🎭"
        type_str = "ПЕРСОНАЖ"
    else:
        type_str = "ЗАПРОС"
        emoji = "❓"
    
    print("\n" + "=" * 80)
    print(f"🔔 НОВЫЙ ЗАПРОС [{timestamp}]")
    print("=" * 80)
    print(f"👤 Пользователь: {user_name}")
    print(f"🆔 ID: {user_id}")
    print(f"📊 Тип: {emoji} {type_str}")
    print(f"📝 Содержание: {content}")
    print("-" * 80)
    print(f"📈 Статистика:")
    print(f"   • Всего запросов: {total_requests}")
    print(f"   • 📚 Произведений: {work_requests}")
    print(f"   • 🎭 Персонажей: {character_requests}")
    print("=" * 80 + "\n")

def log_api_response(model, response_length, success=True):
    """Логирует ответ от API"""
    if success:
        status = "✅ УСПЕХ"
    else:
        status = "❌ ОШИБКА"
    
    short_name = model.split('/')[-1].replace(':free', '') if model else "unknown"
    print(f"  ↳ {status} | Модель: {short_name} | Длина: {response_length} символов")

def log_error(error_msg):
    """Логирует ошибку"""
    print(f"  ↳ ❌ ОШИБКА: {error_msg}")

# ============================================
# ФУНКЦИЯ ДЛЯ ОЧИСТКИ ТЕКСТА ОТ MARKDOWN
# ============================================

def remove_markdown(text):
    """Полностью удаляет Markdown-разметку, оставляя только текст"""
    if not text:
        return text
    
    text = re.sub(r'[*_`~]', '', text)
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
    text = re.sub(r'!\[[^\]]*\]\([^\)]+\)', '', text)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^[-*+]\s+', '• ', text, flags=re.MULTILINE)
    text = re.sub(r'^\d+\.\s+', '• ', text, flags=re.MULTILINE)
    
    return text

def clean_text_for_telegram(text):
    """Очищает текст для отправки в Telegram (без Markdown)"""
    if not text:
        return text
    
    text = remove_markdown(text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    
    return text

# ============================================
# ФУНКЦИЯ РАЗБИЕНИЯ ДЛИННЫХ СООБЩЕНИЙ
# ============================================

def split_long_message(text, max_length=3500):
    """Разбивает длинное сообщение на части"""
    if len(text) <= max_length:
        return [text]
    
    parts = []
    remaining = text
    
    while len(remaining) > max_length:
        split_point = remaining[:max_length].rfind('\n\n')
        if split_point == -1:
            split_point = remaining[:max_length].rfind('\n')
        if split_point == -1:
            split_point = remaining[:max_length].rfind('. ')
        if split_point == -1:
            split_point = remaining[:max_length].rfind(' ')
        if split_point == -1:
            split_point = max_length
        else:
            split_point += 1
        
        parts.append(remaining[:split_point].strip())
        remaining = remaining[split_point:].strip()
    
    if remaining:
        parts.append(remaining)
    
    return parts

async def safe_send_message(message: Message, text: str):
    """Безопасная отправка сообщений без Markdown"""
    try:
        clean_text = clean_text_for_telegram(text)
        parts = split_long_message(clean_text)
        
        for i, part in enumerate(parts):
            if i == 0:
                await message.reply(part)
            else:
                await asyncio.sleep(0.7)
                await message.answer(f"📌 Продолжение {i+1}/{len(parts)}:\n\n{part}")
                    
    except Exception as e:
        logger.error(f"Ошибка отправки: {e}")
        await message.reply(f"⚠️ Ошибка при отправке сообщения. Попробуйте еще раз.")

# ============================================
# ФУНКЦИИ ДЛЯ РАБОТЫ С OPENROUTER
# ============================================

async def fetch_available_models():
    """Получает список доступных бесплатных моделей"""
    global available_models, current_model
    
    try:
        # Используем синхронный вызов в отдельном потоке
        response = await asyncio.to_thread(client.models.list)
        
        free_models = []
        for model in response.data:
            if hasattr(model, 'id') and ':free' in model.id:
                free_models.append(model.id)
        
        if free_models:
            available_models = free_models
            current_model = free_models[0]
            logger.info(f"✅ Найдено {len(free_models)} бесплатных моделей")
            return True, free_models
        else:
            logger.warning("⚠️ Бесплатные модели не найдены, использую запасной список")
            available_models = FALLBACK_MODELS
            current_model = FALLBACK_MODELS[0]
            return True, FALLBACK_MODELS
            
    except Exception as e:
        logger.error(f"❌ Ошибка получения моделей: {e}")
        logger.info("🔄 Использую запасной список моделей")
        available_models = FALLBACK_MODELS
        current_model = FALLBACK_MODELS[0]
        return False, FALLBACK_MODELS

async def analyze_with_openrouter(query, model=None, timeout=45.0):
    """Отправляет запрос к OpenRouter API для анализа произведения"""
    if model is None:
        model = current_model
    
    try:
        # Используем asyncio.wait_for для жесткого контроля времени
        completion = await asyncio.wait_for(
            asyncio.to_thread(
                client.chat.completions.create,
                model=model,
                messages=[
                    {"role": "system", "content": LITERARY_ANALYSIS_PROMPT},
                    {"role": "user", "content": f"Проанализируй произведение: {query}"}
                ],
                temperature=0.7,
                max_tokens=4000,
                top_p=0.9,
                extra_headers={
                    "HTTP-Referer": OPENROUTER_REFERER,
                    "X-Title": OPENROUTER_TITLE,
                }
            ),
            timeout=timeout
        )
        
        analysis = completion.choices[0].message.content
        logger.info(f"✅ Успешный ответ от модели {model}")
        log_api_response(model, len(analysis), True)
        return analysis
        
    except asyncio.TimeoutError:
        log_error(f"Таймаут с моделью {model} ({timeout}с)")
        return None
    except Exception as e:
        log_error(str(e))
        logger.error(f"❌ Ошибка с моделью {model}: {e}")
        return None

async def analyze_character(work, character, model=None, timeout=45.0):
    """Анализирует конкретного персонажа произведения"""
    if model is None:
        model = current_model
    
    try:
        completion = await asyncio.wait_for(
            asyncio.to_thread(
                client.chat.completions.create,
                model=model,
                messages=[
                    {"role": "system", "content": CHARACTER_ANALYSIS_PROMPT.format(work=work, character=character)},
                    {"role": "user", "content": f"Подробно проанализируй персонажа {character} из произведения {work}"}
                ],
                temperature=0.7,
                max_tokens=4000,
                top_p=0.9,
                extra_headers={
                    "HTTP-Referer": OPENROUTER_REFERER,
                    "X-Title": OPENROUTER_TITLE,
                }
            ),
            timeout=timeout
        )
        
        analysis = completion.choices[0].message.content
        logger.info(f"✅ Анализ персонажа {character}")
        log_api_response(model, len(analysis), True)
        return analysis
        
    except asyncio.TimeoutError:
        log_error(f"Таймаут при анализе персонажа {character} ({timeout}с)")
        return None
    except Exception as e:
        log_error(str(e))
        logger.error(f"❌ Ошибка при анализе персонажа: {e}")
        return None

# ============================================
# ОБРАБОТЧИКИ КОМАНД
# ============================================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    welcome_text = f"""
📚 Добро пожаловать в Литературного аналитика!

🌐 OpenRouter API: активен
🤖 Моделей в списке: {len(available_models) if available_models else 'загружается...'}

🎭 Анализ персонажей через /character

Основные команды:
/character [имя] - анализ персонажа
/models - список доступных моделей
/model [номер] - выбрать модель
/stats - статистика
/help - справка

⏱️ Время анализа: 45-60 секунд
"""
    await message.reply(welcome_text)
    log_user_request(message.from_user, "command", "/start")

@dp.message(Command("help"))
async def cmd_help(message: Message):
    help_text = """
📚 Как пользоваться ботом:

1. Отправьте название произведения - получите полный анализ
2. /character Имя - анализ конкретного персонажа

Примеры:
• "Война и мир"
• "Преступление и наказание"
• /character Наташа Ростова
• /character Раскольников

Команды:
/character [имя] - анализ персонажа
/models - список моделей
/model [номер] - выбрать модель
/stats - статистика
/about - информация
"""
    await message.reply(help_text)
    log_user_request(message.from_user, "command", "/help")

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    """Показывает статистику запросов"""
    global total_requests, work_requests, character_requests, current_model, available_models
    
    current_name = current_model.split('/')[-1].replace(':free', '') if current_model else 'не выбрана'
    
    stats_text = f"""
📊 Статистика бота:

📈 Всего запросов: {total_requests}
📚 Произведений: {work_requests}
🎭 Персонажей: {character_requests}

🤖 Текущая модель: {current_name}
📋 Моделей в списке: {len(available_models)}
⏱️ Таймаут: {HTTP_TIMEOUT}с
"""
    await message.reply(stats_text)
    log_user_request(message.from_user, "command", "/stats")

@dp.message(Command("character"))
async def cmd_character(message: Message, state: FSMContext):
    """Обработчик команды /character для анализа персонажа"""
    
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply(
            "❌ Укажите имя персонажа\n\n"
            "Примеры:\n"
            "/character Наташа Ростова\n"
            "/character Раскольников\n"
            "/character Воланд"
        )
        return
    
    character_name = args[1].strip()
    
    log_user_request(message.from_user, "character", character_name)
    
    await state.update_data(character=character_name)
    await state.set_state(AnalysisStates.waiting_for_character)
    
    await message.reply(
        f"🎭 Персонаж: {character_name}\n\n"
        f"Теперь укажите произведение, в котором этот персонаж встречается.\n"
        f"Или отправьте /cancel для отмены."
    )

@dp.message(AnalysisStates.waiting_for_character)
async def process_character_analysis(message: Message, state: FSMContext):
    """Обрабатывает запрос на анализ персонажа"""
    global current_model, available_models
    
    data = await state.get_data()
    character = data.get('character')
    work = message.text
    
    await state.clear()
    
    await bot.send_chat_action(message.chat.id, action="typing")
    
    log_user_request(message.from_user, "character_work", f"{character} из {work}")
    
    status_msg = await message.reply(f"🔄 Анализирую персонажа... ⏱️ до 45 сек")
    
    # Пробуем текущую модель
    analysis = await analyze_character(work, character, current_model, timeout=40.0)
    
    # Если не получилось, пробуем другие модели
    if not analysis and available_models:
        for model in available_models:
            if model != current_model:
                analysis = await analyze_character(work, character, model, timeout=35.0)
                if analysis:
                    current_model = model
                    break
    
    if analysis:
        await status_msg.delete()
        model_short = current_model.split('/')[-1].replace(':free', '')
        response = f"🎭 {character}\n📚 {work}\n\n{analysis}\n\n---\n🤖 {model_short}"
        await safe_send_message(message, response)
    else:
        await status_msg.edit_text(
            "❌ Не удалось получить ответ. Попробуйте позже или другую модель (/models)."
        )

@dp.message(Command("models"))
async def cmd_models(message: Message):
    global available_models, current_model
    
    if not available_models:
        await message.reply("🔄 Получаю список моделей...")
        await fetch_available_models()
    
    if available_models:
        models_list = "\n".join([f"{i+1}. {model.split('/')[-1].replace(':free', '')}" for i, model in enumerate(available_models[:15])])
        current_idx = available_models.index(current_model) + 1 if current_model in available_models else 1
        current_name = current_model.split('/')[-1].replace(':free', '')
        
        await message.reply(
            f"🤖 *Доступные бесплатные модели:*\n\n{models_list}\n\n"
            f"Текущая модель: {current_idx}. {current_name}\n\n"
            f"Используйте /model [номер] чтобы выбрать модель\n"
            f"Например: /model 1",
            parse_mode="Markdown"
        )
    else:
        await message.reply("❌ Не удалось получить список моделей. Попробуйте позже.")
    
    log_user_request(message.from_user, "command", "/models")

@dp.message(Command("model"))
async def cmd_model(message: Message):
    global current_model, available_models
    
    if not available_models:
        await fetch_available_models()
    
    args = message.text.split()
    if len(args) < 2:
        current_name = current_model.split('/')[-1].replace(':free', '') if current_model else 'не выбрана'
        await message.reply(
            f"❌ Укажите номер модели\n\n"
            f"Текущая модель: {current_name}\n"
            f"Список моделей: /models"
        )
        return
    
    try:
        model_idx = int(args[1]) - 1
        if 0 <= model_idx < len(available_models):
            old_model = current_model
            current_model = available_models[model_idx]
            model_name = current_model.split('/')[-1].replace(':free', '')
            await message.reply(f"✅ Модель изменена на: {model_name}")
            log_user_request(message.from_user, "command", f"/model {args[1]}")
        else:
            await message.reply(f"❌ Неверный номер. Доступны номера 1-{len(available_models)}")
    except ValueError:
        await message.reply("❌ Укажите число (номер модели из списка /models)")

@dp.message(Command("about"))
async def cmd_about(message: Message):
    global available_models
    
    about_text = f"""
🤖 О боте

Версия: 10.3 (С подробным анализом)
Моделей в списке: {len(available_models)}
Таймаут: {HTTP_TIMEOUT}с

Возможности:
📚 Подробный анализ произведений (10+ предложений)
🎭 Глубокий анализ персонажей (6 аспектов)
🔄 Автоматическое получение моделей

Команды:
/character - анализ персонажа
/models - список моделей
/model [номер] - выбрать модель
/stats - статистика

👨‍💻 by @aventt0
"""
    await message.reply(about_text)
    log_user_request(message.from_user, "command", "/about")

@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    """Отмена текущего действия"""
    current_state = await state.get_state()
    if current_state is None:
        await message.reply("❌ Нет активного действия")
        return
    
    await state.clear()
    await message.reply("✅ Действие отменено")
    log_user_request(message.from_user, "command", "/cancel")

# ============================================
# ОСНОВНОЙ ОБРАБОТЧИК
# ============================================

@dp.message()
async def analyze_literature(message: Message, state: FSMContext):
    global current_model, available_models, work_requests
    
    try:
        await bot.send_chat_action(message.chat.id, action="typing")
        
        query = message.text
        
        if query.startswith('/'):
            return
        
        log_user_request(message.from_user, "work", query)
        
        # Сохраняем последний запрос
        last_query[message.from_user.id] = query
        
        # Проверяем наличие моделей
        if not available_models:
            await fetch_available_models()
        
        status_msg = await message.reply("🔄 Анализирую... ⏱️ до 45 сек")
        
        # Пробуем текущую модель
        analysis = await analyze_with_openrouter(query, current_model, timeout=60.0)
        
        # Если не получилось, пробуем другие модели
        if not analysis and available_models:
            for model in available_models:
                if model != current_model:
                    analysis = await analyze_with_openrouter(query, model, timeout=50.0)
                    if analysis:
                        current_model = model
                        break
        
        if analysis:
            await status_msg.delete()
            model_short = current_model.split('/')[-1].replace(':free', '')
            response = f"📚 {query}\n\n{analysis}\n\n---\n🤖 {model_short}\n\n💡 Чтобы узнать о персонаже: /character Имя"
            await safe_send_message(message, response)
        else:
            await status_msg.edit_text(
                "❌ Не удалось получить ответ.\n"
                "Попробуйте позже или другую модель: /models"
            )
        
    except Exception as e:
        log_error(str(e))
        logger.error(f"Ошибка: {e}")
        await message.reply("⚠️ Ошибка. Попробуйте еще раз.")

# ============================================
# ЗАПУСК БОТА
# ============================================

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    
    try:
        bot_info = await bot.me()
        bot_username = bot_info.username
    except:
        bot_username = "unknown"
    
    print("\n" + "=" * 80)
    print("🚀 БОТ ЗАПУСКАЕТСЯ")
    print("=" * 80)
    print(f"🤖 Bot: @{bot_username}")
    print(f"🔑 OpenRouter Key: ✅ Есть")
    print(f"⏱️ Таймаут: {HTTP_TIMEOUT}с")
    print("-" * 80)
    
    print("🔄 Получение списка бесплатных моделей...")
    success, models = await fetch_available_models()
    
    if models:
        print(f"✅ Найдено {len(models)} бесплатных моделей:")
        for i, model in enumerate(models[:8], 1):
            short_name = model.split('/')[-1].replace(':free', '')
            print(f"   {i}. {short_name}")
        if len(models) > 8:
            print(f"   ... и ещё {len(models) - 8}")
        
        short_current = current_model.split('/')[-1].replace(':free', '')
        print(f"\n🤖 Текущая модель: {short_current}")
        print(f"\n📋 Режим: Подробный анализ (старый промпт)")
    else:
        print("❌ Не удалось получить список моделей")
        print("🔄 Использую запасной список")
    
    print("=" * 80)
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n" + "=" * 80)
        print("👋 Бот остановлен")
        print(f"📊 Итоговая статистика:")
        print(f"   • Всего запросов: {total_requests}")
        print(f"   • 📚 Произведений: {work_requests}")
        print(f"   • 🎭 Персонажей: {character_requests}")
        print("=" * 80)
        http_client.close()
