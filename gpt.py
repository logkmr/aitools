import asyncio
import logging
import json
import os
import base64
import io
import aiohttp
from aiogram import Bot, Dispatcher, types, F, Router
from aiogram.filters import Command
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from typing import Dict, Any, List

# Импортируем pypdf для работы с PDF
try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Токены API
API_TOKEN = os.getenv("API_TOKEN")
OCR_API_TOKEN = os.getenv("OCR_API_TOKEN")

if not API_TOKEN:
    raise RuntimeError("API_TOKEN не найден в .env файле")

# Инициализация
bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

# Файл для хранения данных пользователей
USER_DATA_FILE = "users.json"

# Словарь для сессий пользователей (история чата: user_id -> list)
user_sessions = {}

# Словарь для фото-сессий (user_id -> {'photos': [], 'last_message_id': None})
photo_sessions = {}


class UserStorage:
    def __init__(self, filename: str):
        self.filename = filename
        self._ensure_file_exists()

    def _ensure_file_exists(self):
        """Создает файл если он не существует"""
        if not os.path.exists(self.filename):
            with open(self.filename, 'w', encoding='utf-8') as f:
                json.dump({}, f)

    def load_users(self) -> Dict[str, Any]:
        """Загружает данные пользователей из JSON файла"""
        try:
            with open(self.filename, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return {}

    def save_users(self, users_data: Dict[str, Any]):
        """Сохраняет данные пользователей в JSON файл"""
        with open(self.filename, 'w', encoding='utf-8') as f:
            json.dump(users_data, f, ensure_ascii=False, indent=2)

    def _migrate_old_settings(self, user_data: Dict[str, Any]) -> Dict[str, Any]:
        """Мигрирует старые настройки в новую структуру профилей"""
        if 'profiles' not in user_data:
            old_settings = user_data.copy()
            return {
                'profiles': {
                    'default': {
                        'baseurl': old_settings.get('baseurl'),
                        'apikey': old_settings.get('apikey'),
                        'model': old_settings.get('model'),
                        'max_tokens': old_settings.get('max_tokens', 2000),
                        'photo_processing': old_settings.get('photo_processing', 'text'),
                        'model_prompt': old_settings.get('model_prompt', ''),
                        'thinking_budget': old_settings.get('thinking_budget', 'off')
                    }
                },
                'active_profile': 'default',
                'response_format': old_settings.get('response_format', 'normal'),
                'split_response': old_settings.get('split_response', False),
                'ask_before_send_photos': old_settings.get('ask_before_send_photos', True)
            }
        
        if 'ask_before_send_photos' not in user_data and 'profiles' in user_data:
            ask_photos_value = True
            for profile_name, profile_data in user_data['profiles'].items():
                if 'ask_before_send_photos' in profile_data:
                    ask_photos_value = profile_data['ask_before_send_photos']
                    break
            user_data['ask_before_send_photos'] = ask_photos_value
            for profile_name in user_data['profiles']:
                user_data['profiles'][profile_name].pop('ask_before_send_photos', None)
        
        return user_data

    def get_user_data(self, user_id: int) -> Dict[str, Any]:
        """Возвращает полные данные пользователя с миграцией при необходимости"""
        users = self.load_users()
        raw_data = users.get(str(user_id), {})
        return self._migrate_old_settings(raw_data)

    def get_active_profile_settings(self, user_id: int) -> Dict[str, Any]:
        """Возвращает настройки активного профиля + общие параметры"""
        user_data = self.get_user_data(user_id)
        active_name = user_data.get('active_profile', 'default')
        profile = user_data['profiles'].get(active_name, {})

        return {
            **profile,
            'response_format': user_data.get('response_format', 'normal'),
            'split_response': user_data.get('split_response', False),
            'ask_before_send_photos': user_data.get('ask_before_send_photos', True),
            '_active_profile_name': active_name,
            '_all_profiles': list(user_data['profiles'].keys()),
            '_user_id': user_id
        }

    def set_user_data(self, user_id: int, user_data: Dict[str, Any]):
        """Сохраняет полные данные пользователя"""
        users = self.load_users()
        users[str(user_id)] = user_data
        self.save_users(users)

    def set_profile_setting(self, user_id: int, key: str, value: Any):
        """Устанавливает параметр в активном профиле"""
        user_data = self.get_user_data(user_id)
        active = user_data.get('active_profile', 'default')
        if active not in user_data['profiles']:
            user_data['profiles'][active] = {}
        user_data['profiles'][active][key] = value
        self.set_user_data(user_id, user_data)

    def set_common_setting(self, user_id: int, key: str, value: Any):
        """Устанавливает общий параметр"""
        user_data = self.get_user_data(user_id)
        user_data[key] = value
        self.set_user_data(user_id, user_data)

    def create_profile(self, user_id: int, profile_name: str):
        """Создаёт новый профиль"""
        user_data = self.get_user_data(user_id)
        if profile_name in user_data['profiles']:
            raise ValueError("Профиль с таким именем уже существует")
        user_data['profiles'][profile_name] = {
            'baseurl': '',
            'apikey': '',
            'model': '',
            'max_tokens': 2000,
            'photo_processing': 'text',
            'model_prompt': '',
            'thinking_budget': 'off'
        }
        self.set_user_data(user_id, user_data)

    def delete_profile(self, user_id: int, profile_name: str):
        """Удаляет профиль"""
        user_data = self.get_user_data(user_id)
        if len(user_data['profiles']) <= 1:
            raise ValueError("Нельзя удалить единственный профиль")
        if profile_name == user_data.get('active_profile'):
            raise ValueError("Нельзя удалить активный профиль")
        user_data['profiles'].pop(profile_name, None)
        self.set_user_data(user_id, user_data)

    def switch_profile(self, user_id: int, profile_name: str):
        """Переключает активный профиль"""
        user_data = self.get_user_data(user_id)
        if profile_name not in user_data['profiles']:
            raise ValueError("Профиль не найден")
        user_data['active_profile'] = profile_name
        self.set_user_data(user_id, user_data)

    def rename_profile(self, user_id: int, old_name: str, new_name: str):
        """Переименовывает профиль"""
        user_data = self.get_user_data(user_id)
        if new_name in user_data['profiles']:
            raise ValueError("Профиль с таким именем уже существует")
        if old_name not in user_data['profiles']:
            raise ValueError("Исходный профиль не найден")
        profile = user_data['profiles'].pop(old_name)
        user_data['profiles'][new_name] = profile
        if user_data.get('active_profile') == old_name:
            user_data['active_profile'] = new_name
        self.set_user_data(user_id, user_data)


# Инициализация хранилища
user_storage = UserStorage(USER_DATA_FILE)


# Состояния
class ProfileStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_baseurl = State()
    waiting_for_apikey = State()
    waiting_for_model = State()
    waiting_for_max_tokens = State()
    waiting_for_rename = State()
    waiting_for_delete_confirm = State()
    waiting_for_model_prompt = State()


class SettingsStates(StatesGroup):
    baseurl = State()
    apikey = State()
    model = State()
    max_tokens = State()


class TransferStates(StatesGroup):
    waiting_for_vk_id = State()


# ————————————————————————————————————————————————————————
# UI Utilities
# ————————————————————————————————————————————————————————

def get_profile_settings_text(settings: Dict[str, Any]) -> str:
    response_format_text = {
        'normal': 'Обычный',
        'medium': 'Средний',
        'short': 'Короткий'
    }.get(settings.get('response_format', 'normal'), 'Обычный')
    
    split_text = "✅ Включено" if settings.get('split_response', False) else "❌ Выключено"
    
    photo_processing_text = {
        'text': '📝 Как текст (OCR)',
        'image': '🖼️ Как фото (Vision)'
    }.get(settings.get('photo_processing', 'text'), '📝 Как текст (OCR)')
    
    ask_before_send_text = "✅ Спрашивать" if settings.get('ask_before_send_photos', True) else "❌ Отправлять сразу"
    
    model_prompt = settings.get('model_prompt', '')
    model_prompt_text = f"{model_prompt[:50]}..." if len(model_prompt) > 50 else model_prompt if model_prompt else "Не установлен"
    
    thinking_budget_text = {
        'off': '❌ Выключено',
        'low': '🔹 Низкое (1024)',
        'medium': '🔶 Среднее (8192)',
        'high': '🔴 Высокое (24576)'
    }.get(settings.get('thinking_budget', 'off'), '❌ Выключено')
    
    settings_text = (
        f"⚙️ <b>Текущие настройки:</b>\n"
        f"👤 Профиль: {settings.get('_active_profile_name', '—')}\n"
        f"🔗 Base URL: {settings.get('baseurl', 'Не установлен')}\n"
        f"🔑 API ключ: •••{settings.get('apikey', '')[-4:] if settings.get('apikey') else 'Не установлен'}\n"
        f"🤖 Модель: {settings.get('model', 'Не установлена')}\n"
        f"📊 Макс. токенов: {settings.get('max_tokens', 2000)}\n"
        f"📝 Формат ответа: {response_format_text}\n"
        f"📋 Разделение ответа: {split_text}\n"
        f"🖼️ Принять фото как: {photo_processing_text}\n"
        f"📸 Подтверждение отправки фото: {ask_before_send_text}\n"
        f"🧠 Глубина мышления: {thinking_budget_text}\n"
        f"💬 Промпт модели: {model_prompt_text}\n"
        f"Выберите параметр для изменения:"
    )
    return settings_text


def get_profile_keyboard(settings: Dict[str, Any]) -> types.InlineKeyboardMarkup:
    response_format = settings.get('response_format', 'normal')
    split_response = settings.get('split_response', False)

    # Кнопки в один ряд: изменить промпт, очистить промпт
    prompt_buttons = [
        [
            types.InlineKeyboardButton(text="💬 Изменить промпт", callback_data="change_model_prompt"),
            types.InlineKeyboardButton(text="🗑️ Очистить промпт", callback_data="clear_model_prompt")
        ]
    ]

    # Кнопка смены профиля отдельно внизу
    profile_button = [
        [types.InlineKeyboardButton(text="👤 Сменить профиль", callback_data="manage_profiles")]
    ]

    # Кнопки формата ответа в один ряд
    format_buttons = [
        [
            types.InlineKeyboardButton(text=f"{'✅' if response_format == 'normal' else '○'} Обычный", callback_data="format_normal"),
            types.InlineKeyboardButton(text=f"{'✅' if response_format == 'medium' else '○'} Средний", callback_data="format_medium"),
            types.InlineKeyboardButton(text=f"{'✅' if response_format == 'short' else '○'} Короткий", callback_data="format_short")
        ]
    ]

    # Кнопки разделения ответа в один ряд
    split_buttons = [
        [
            types.InlineKeyboardButton(text=f"{'✅' if split_response else '○'} Разделять", callback_data="split_true"),
            types.InlineKeyboardButton(text=f"{'✅' if not split_response else '○'} Не разделять", callback_data="split_false")
        ]
    ]

    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="⚙️ Параметры запроса", callback_data="show_request_params")],
        [types.InlineKeyboardButton(text="📝 Формат ответа", callback_data="show_formats")],
        *format_buttons,
        [types.InlineKeyboardButton(text="📋 Разделение ответа", callback_data="show_split")],
        *split_buttons,
        *prompt_buttons,
        *profile_button
    ])
    return keyboard


def get_request_params_keyboard(settings: Dict[str, Any]) -> types.InlineKeyboardMarkup:
    photo_processing = settings.get('photo_processing', 'text')
    thinking_budget = settings.get('thinking_budget', 'off')
    
    photo_buttons = [
        [
            types.InlineKeyboardButton(text=f"{'✅' if photo_processing == 'text' else '○'} Как текст (OCR)", callback_data="photo_text"),
            types.InlineKeyboardButton(text=f"{'✅' if photo_processing == 'image' else '○'} Как фото (Vision)", callback_data="photo_image")
        ]
    ]

    ask_photo_buttons = [
        [
            types.InlineKeyboardButton(text=f"{'✅' if settings.get('ask_before_send_photos', True) else '○'} Спрашивать", callback_data="ask_photo_true"),
            types.InlineKeyboardButton(text=f"{'✅' if not settings.get('ask_before_send_photos', True) else '○'} Отправлять сразу", callback_data="ask_photo_false")
        ]
    ]

    thinking_buttons = [
        [
            types.InlineKeyboardButton(text=f"{'✅' if thinking_budget == 'off' else '○'} Выкл", callback_data="thinking_off"),
            types.InlineKeyboardButton(text=f"{'✅' if thinking_budget == 'low' else '○'} Низкое", callback_data="thinking_low"),
            types.InlineKeyboardButton(text=f"{'✅' if thinking_budget == 'medium' else '○'} Среднее", callback_data="thinking_medium"),
            types.InlineKeyboardButton(text=f"{'✅' if thinking_budget == 'high' else '○'} Высокое", callback_data="thinking_high"),
        ]
    ]

    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🔗 Изменить Base URL", callback_data="change_baseurl")],
        [types.InlineKeyboardButton(text="🔑 Изменить API ключ", callback_data="change_apikey")],
        [types.InlineKeyboardButton(text="🤖 Изменить модель", callback_data="change_model")],
        [types.InlineKeyboardButton(text="📊 Изменить макс. токены", callback_data="change_tokens")],
        [types.InlineKeyboardButton(text="🖼️ Обработка фото", callback_data="show_photo_processing")],
        *photo_buttons,
        [types.InlineKeyboardButton(text="📸 Подтверждение отправки фото", callback_data="show_ask_photo")],
        *ask_photo_buttons,
        [types.InlineKeyboardButton(text="🧠 Глубина мышления", callback_data="show_thinking")],
        *thinking_buttons,
        [types.InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_settings")]
    ])
    return keyboard


def get_profiles_manager_keyboard(profiles: List[str], active: str) -> types.InlineKeyboardMarkup:
    buttons = []
    for name in profiles:
        mark = "✅ " if name == active else "○ "
        row_buttons = [
            types.InlineKeyboardButton(text=f"{mark}{name}", callback_data=f"switch_profile_{name}"),
            types.InlineKeyboardButton(text="✏️", callback_data=f"rename_profile_{name}"),
            types.InlineKeyboardButton(text="🗑️", callback_data=f"delete_profile_{name}")
        ]
        buttons.append(row_buttons)
    
    buttons.append([types.InlineKeyboardButton(text="➕ Создать профиль", callback_data="create_profile")])
    buttons.append([types.InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_settings")])
    return types.InlineKeyboardMarkup(inline_keyboard=buttons)


def get_album_confirmation_keyboard() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(text="✅ Отправить", callback_data="album_send"),
            types.InlineKeyboardButton(text="❌ Прекратить", callback_data="album_cancel")
        ]
    ])


# ————————————————————————————————————————————————————————
# Command Handlers
# ————————————————————————————————————————————————————————

@router.message(Command("start"))
async def cmd_start(message: types.Message):
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="📤 Перенести профили в ВК бота", callback_data="transfer_to_vk")]
    ])
    await message.answer(
        "🤖 Бот для работы с ChatGPT\n"
        "Доступные команды:\n"
        "/settings — Настройки API\n"
        "/show_settings — Показать настройки\n"
        "/clear — Очистить контекст беседы\n"
        "Отправьте текст, фото или файл (код, текст) — бот ответит!\nБот в ВК - https://vk.ru/club235624714",
        reply_markup=keyboard
    )


@router.message(Command("clear"))
async def cmd_clear(message: types.Message):
    user_id = message.from_user.id
    if user_id in user_sessions:
        user_sessions[user_id] = []
    await message.answer("🧹 Контекст беседы очищен.")


@router.callback_query(F.data == "transfer_to_vk")
async def transfer_to_vk_handler(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "📤 Перенос профилей в ВК бота\nВведите ваш ID пользователя ВКонтакте (число)."
    )
    await state.set_state(TransferStates.waiting_for_vk_id)
    await callback.answer()


@router.message(TransferStates.waiting_for_vk_id)
async def process_vk_id(message: types.Message, state: FSMContext):
    try:
        vk_id = int(message.text.strip())
        if vk_id <= 0: raise ValueError
    except ValueError:
        await message.answer("❌ Введите корректный ID (число).")
        await state.clear()
        return
    
    user_id = message.from_user.id
    user_data = user_storage.get_user_data(user_id)
    if not user_data or not user_data.get('profiles'):
        await message.answer("❌ Нет профилей для переноса.")
        await state.clear()
        return
    
    vk_storage = UserStorage("users_vk.json")
    vk_users = vk_storage.load_users()
    vk_users[str(vk_id)] = user_data
    vk_storage.save_users(vk_users)
    
    await message.answer(f"✅ Профили перенесены! ID ВК: {vk_id}")
    await state.clear()


@router.message(Command("settings"))
async def cmd_settings(message: types.Message):
    settings = user_storage.get_active_profile_settings(message.from_user.id)
    await message.answer(
        get_profile_settings_text(settings),
        reply_markup=get_profile_keyboard(settings),
        parse_mode='HTML'
    )

@router.message(F.document)
async def handle_document(message: types.Message):
    user_id = message.from_user.id
    settings = user_storage.get_active_profile_settings(user_id)
    
    if not settings.get('baseurl'):
        await message.answer("❌ Сначала введите настройки (/settings)")
        return

    doc = message.document
    processing_msg = await message.answer(f"📥 Скачиваю файл «{doc.file_name}»...")

    try:
        # Скачиваем файл в память
        file = await bot.get_file(doc.file_id)
        file_io = io.BytesIO()
        await bot.download_file(file.file_path, file_io)
        file_io.seek(0) # Возвращаемся в начало файла

        extracted_text = ""
        error_msg = ""

        # Сценарий 1: Это PDF
        if doc.mime_type == 'application/pdf':
            if PdfReader is None:
                await processing_msg.edit_text("❌ Библиотека pypdf не установлена. Владелец бота должен выполнить: `pip install pypdf`", parse_mode='Markdown')
                return
            
            try:
                reader = PdfReader(file_io)
                text_pages = []
                for i, page in enumerate(reader.pages):
                    page_text = page.extract_text()
                    if page_text:
                        text_pages.append(f"[Страница {i+1}]:\n{page_text}")
                
                extracted_text = "\n\n".join(text_pages)
                if not extracted_text:
                    error_msg = "⚠️ Текст в PDF не найден. Возможно, это скан (картинки без текстового слоя)."
            except Exception as e:
                error_msg = f"Ошибка чтения PDF: {str(e)}"

        # Сценарий 2: Это изображение файлом (png, jpg)
        elif doc.mime_type and doc.mime_type.startswith('image/'):
            # Передаем в обработчик фото как Vision или OCR
            # Для простоты здесь: если это фото-файл, конвертируем в base64 и шлем как картинку
            image_base64 = base64.b64encode(file_io.read()).decode('utf-8')
            prompt = message.caption or "Что в этом файле?"
            await processing_msg.edit_text(f"🖼️ Анализирую изображение через {settings['model']}...")
            try:
                ans = await send_image_to_api(settings, image_base64, prompt)
                await processing_msg.delete()
                await send_response(message.chat.id, ans, settings)
                return
            except Exception as e:
                await processing_msg.edit_text(f"❌ Ошибка Vision: {e}")
                return

        # Сценарий 3: Пробуем читать как обычный текст (txt, py, json, md)
        else:
            try:
                extracted_text = file_io.read().decode('utf-8')
            except UnicodeDecodeError:
                error_msg = "❌ Не удалось прочитать файл как текст (неверная кодировка или бинарный файл)."

        # Если возникла ошибка при чтении
        if error_msg:
            await processing_msg.edit_text(error_msg)
            return
        
        # Если текст слишком большой, обрезаем (лимит токенов модели)
        # Грубая оценка: 1 символ ~= 0.5-1 токен. Ограничим на вход ~40000 символов для безопасности
        if len(extracted_text) > 40000:
            extracted_text = extracted_text[:40000] + "\n\n[...Текст обрезан, так как он слишком длинный...]"

        # Формируем запрос
        user_query = message.caption or "Проанализируй этот файл и сделай краткую выжимку."
        final_prompt = (
            f"Пользователь загрузил файл: {doc.file_name}\n"
            f"Содержимое файла:\n'''\n{extracted_text}\n'''\n\n"
            f"Запрос пользователя: {user_query}"
        )

        await processing_msg.edit_text(f"🔄 Читаю файл и отправляю в {settings['model']}...")
        response = await send_to_chatgpt(settings, final_prompt)
        
        await processing_msg.delete()
        await send_response(message.chat.id, response, settings)

    except Exception as e:
        logger.error(f"File error: {e}")
        await processing_msg.edit_text(f"❌ Критическая ошибка: {e}")


# ————————————————————————————————————————————————————————
# Profile Management Handlers
# ————————————————————————————————————————————————————————

@router.callback_query(F.data == "manage_profiles")
async def manage_profiles(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    settings = user_storage.get_active_profile_settings(user_id)
    try:
        await callback.message.edit_text(
            "👤 <b>Управление профилями</b>",
            reply_markup=get_profiles_manager_keyboard(settings['_all_profiles'], settings['_active_profile_name']),
            parse_mode='HTML'
        )
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data == "back_to_settings")
async def back_to_settings(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    settings = user_storage.get_active_profile_settings(user_id)
    try:
        await callback.message.edit_text(
            get_profile_settings_text(settings),
            reply_markup=get_profile_keyboard(settings),
            parse_mode='HTML'
        )
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data == "show_request_params")
async def show_request_params(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    settings = user_storage.get_active_profile_settings(user_id)
    try:
        await callback.message.edit_text(
            get_profile_settings_text(settings),
            reply_markup=get_request_params_keyboard(settings),
            parse_mode='HTML'
        )
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("switch_profile_"))
async def switch_profile_handler(callback: types.CallbackQuery):
    profile_name = callback.data.replace("switch_profile_", "")
    user_id = callback.from_user.id
    try:
        user_storage.switch_profile(user_id, profile_name)
        settings = user_storage.get_active_profile_settings(user_id)
        try:
            await callback.message.edit_text(
                get_profile_settings_text(settings),
                reply_markup=get_profile_keyboard(settings),
                parse_mode='HTML'
            )
        except Exception:
            pass
        await callback.answer("✅ Профиль переключён")
    except Exception as e:
        await callback.answer(f"Ошибка: {str(e)}", show_alert=True)


@router.callback_query(F.data.startswith("delete_profile_"))
async def delete_profile_handler(callback: types.CallbackQuery):
    profile_name = callback.data.replace("delete_profile_", "")
    user_id = callback.from_user.id
    try:
        user_storage.delete_profile(user_id, profile_name)
        settings = user_storage.get_active_profile_settings(user_id)
        try:
            await callback.message.edit_text(
                "👤 <b>Управление профилями</b>",
                reply_markup=get_profiles_manager_keyboard(settings['_all_profiles'], settings['_active_profile_name']),
                parse_mode='HTML'
            )
        except Exception:
            pass
        await callback.answer(f"✅ Профиль удалён")
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)


@router.callback_query(F.data.startswith("rename_profile_"))
async def rename_profile_handler(callback: types.CallbackQuery, state: FSMContext):
    old_name = callback.data.replace("rename_profile_", "")
    await callback.message.answer(f"Введите новое имя для профиля «{old_name}»:")
    await state.set_state(ProfileStates.waiting_for_rename)
    await state.update_data(old_name=old_name)
    await callback.answer()


@router.message(ProfileStates.waiting_for_rename)
async def process_profile_rename(message: types.Message, state: FSMContext):
    new_name = message.text.strip()
    if not new_name: return
    
    user_data = await state.get_data()
    old_name = user_data['old_name']
    user_id = message.from_user.id
    
    try:
        user_storage.rename_profile(user_id, old_name, new_name)
        settings = user_storage.get_active_profile_settings(user_id)
        await message.answer(
            f"✅ Профиль переименован",
            reply_markup=get_profiles_manager_keyboard(settings['_all_profiles'], settings['_active_profile_name']),
            parse_mode='HTML'
        )
        await state.clear()
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")


@router.callback_query(F.data == "create_profile")
async def create_profile_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите имя нового профиля:")
    await state.set_state(ProfileStates.waiting_for_name)
    await callback.answer()


@router.message(ProfileStates.waiting_for_name)
async def create_profile_name(message: types.Message, state: FSMContext):
    name = message.text.strip()
    if not name: return
    user_id = message.from_user.id
    try:
        user_storage.create_profile(user_id, name)
        user_storage.switch_profile(user_id, name)
        settings = user_storage.get_active_profile_settings(user_id)
        await message.answer(
            f"✅ Профиль «{name}» создан",
            reply_markup=get_profile_keyboard(settings),
            parse_mode='HTML'
        )
        await state.clear()
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")


# ————————————————————————————————————————————————————————
# Settings Changes Handlers
# ————————————————————————————————————————————————————————

@router.callback_query(F.data.startswith("change_"))
async def handle_settings_change(callback: types.CallbackQuery, state: FSMContext):
    action = callback.data
    if action == "change_baseurl":
        await callback.message.answer("Введите новый Base URL:")
        await state.set_state(SettingsStates.baseurl)
    elif action == "change_apikey":
        await callback.message.answer("Введите новый API ключ:")
        await state.set_state(SettingsStates.apikey)
    elif action == "change_model":
        await callback.message.answer("Введите новую модель:")
        await state.set_state(SettingsStates.model)
    elif action == "change_tokens":
        await callback.message.answer("Введите макс. количество токенов (число):")
        await state.set_state(SettingsStates.max_tokens)
    elif action == "change_photo_processing":
        await callback.answer()
    elif action == "change_model_prompt":
        await callback.message.answer("Введите промпт для модели (/cancel для отмены):")
        await state.set_state(ProfileStates.waiting_for_model_prompt)
    await callback.answer()


@router.callback_query(F.data == "clear_model_prompt")
async def clear_model_prompt(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    user_storage.set_profile_setting(user_id, 'model_prompt', '')
    settings = user_storage.get_active_profile_settings(user_id)
    try:
        await callback.message.edit_text(
            get_profile_settings_text(settings),
            reply_markup=get_profile_keyboard(settings),
            parse_mode='HTML'
        )
    except Exception:
        pass
    await callback.answer("✅ Промпт очищен")


@router.message(ProfileStates.waiting_for_model_prompt)
async def process_model_prompt(message: types.Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Отменено")
        return
    user_storage.set_profile_setting(message.from_user.id, 'model_prompt', message.text.strip())
    await state.clear()
    settings = user_storage.get_active_profile_settings(message.from_user.id)
    await message.answer(
        "✅ Промпт сохранен!\n" + get_profile_settings_text(settings),
        reply_markup=get_profile_keyboard(settings),
        parse_mode='HTML'
    )


@router.message(SettingsStates.baseurl)
async def process_baseurl(message: types.Message, state: FSMContext):
    baseurl = message.text.strip()
    if not baseurl.startswith(('http://', 'https://')):
        await message.answer("❌ URL должен начинаться с http:// или https://")
        return
    user_storage.set_profile_setting(message.from_user.id, 'baseurl', baseurl)
    await state.clear()
    await finish_setting_update(message)


@router.message(SettingsStates.apikey)
async def process_apikey(message: types.Message, state: FSMContext):
    user_storage.set_profile_setting(message.from_user.id, 'apikey', message.text.strip())
    await state.clear()
    await finish_setting_update(message)


@router.message(SettingsStates.model)
async def process_model(message: types.Message, state: FSMContext):
    user_storage.set_profile_setting(message.from_user.id, 'model', message.text.strip())
    await state.clear()
    await finish_setting_update(message)


@router.message(SettingsStates.max_tokens)
async def process_max_tokens(message: types.Message, state: FSMContext):
    try:
        val = int(message.text.strip())
        if val < 100 or val > 100000: raise ValueError
    except ValueError:
        await message.answer("❌ Введите число от 100 до 100000")
        return
    user_storage.set_profile_setting(message.from_user.id, 'max_tokens', val)
    await state.clear()
    await finish_setting_update(message)


async def finish_setting_update(message: types.Message):
    settings = user_storage.get_active_profile_settings(message.from_user.id)
    await message.answer(
        get_profile_settings_text(settings),
        reply_markup=get_request_params_keyboard(settings),
        parse_mode='HTML'
    )


# Toggles
@router.callback_query(F.data.startswith("format_"))
@router.callback_query(F.data.startswith("split_"))
@router.callback_query(F.data.startswith("photo_"))
@router.callback_query(F.data.startswith("ask_photo_"))
@router.callback_query(F.data.startswith("thinking_"))
@router.callback_query(F.data == "show_ask_photo")
@router.callback_query(F.data == "show_formats")
@router.callback_query(F.data == "show_split")
@router.callback_query(F.data == "show_photo_processing")
@router.callback_query(F.data == "show_thinking")
async def handle_toggles(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    data = callback.data

    if data.startswith("format_"):
        user_storage.set_common_setting(user_id, 'response_format', data.replace("format_", ""))
    elif data.startswith("split_"):
        user_storage.set_common_setting(user_id, 'split_response', data == "split_true")
    elif data == "photo_text":
        user_storage.set_profile_setting(user_id, 'photo_processing', 'text')
    elif data == "photo_image":
        user_storage.set_profile_setting(user_id, 'photo_processing', 'image')
    elif data.startswith("ask_photo_"):
        user_storage.set_common_setting(user_id, 'ask_before_send_photos', data == "ask_photo_true")
    elif data.startswith("thinking_"):
        level = data.replace("thinking_", "")  # off / low / medium / high
        user_storage.set_profile_setting(user_id, 'thinking_budget', level)

    settings = user_storage.get_active_profile_settings(user_id)

    # Кнопки из "Параметров запроса" — возвращаемся туда же
    request_param_prefixes = ("photo_", "ask_photo_", "thinking_", "show_photo_processing", "show_ask_photo", "show_thinking")
    if any(data.startswith(p) if p.endswith("_") else data == p for p in request_param_prefixes):
        keyboard = get_request_params_keyboard(settings)
    else:
        keyboard = get_profile_keyboard(settings)

    try:
        await callback.message.edit_text(
            get_profile_settings_text(settings),
            reply_markup=keyboard,
            parse_mode='HTML'
        )
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data == "close_settings")
async def handle_close_settings(callback: types.CallbackQuery):
    await callback.message.delete()
    await callback.answer()


@router.message(Command("show_settings"))
async def cmd_show_settings(message: types.Message):
    settings = user_storage.get_active_profile_settings(message.from_user.id)
    if not settings.get('baseurl'):
        await message.answer("❌ Сначала настройте бот: /settings")
        return
    await message.answer(get_profile_settings_text(settings), parse_mode='HTML')


@router.message(Command("delete_settings"))
async def cmd_delete_settings(message: types.Message):
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="✅ Удалить", callback_data="confirm_delete"),
         types.InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_delete")]
    ])
    await message.answer("⚠️ Удалить все ваши настройки?", reply_markup=keyboard)


@router.callback_query(F.data == "confirm_delete")
async def confirm_delete(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    users = user_storage.load_users()
    users.pop(str(user_id), None)
    user_storage.save_users(users)
    await callback.message.edit_text("✅ Настройки удалены")
    await callback.answer()


@router.callback_query(F.data == "cancel_delete")
async def cancel_delete(callback: types.CallbackQuery):
    await callback.message.edit_text("❌ Отменено")
    await callback.answer()


# ————————————————————————————————————————————————————————
# File / Document Processing
# ————————————————————————————————————————————————————————

@router.message(F.document)
async def handle_document(message: types.Message):
    user_id = message.from_user.id
    settings = user_storage.get_active_profile_settings(user_id)
    
    if not settings.get('baseurl'):
        await message.answer("❌ Сначала настройте бот: /settings")
        return

    doc = message.document
    
    # 1. Проверяем, является ли документ изображением (отправленным как файл)
    if doc.mime_type and doc.mime_type.startswith('image/'):
        # Обрабатываем как картинку
        processing_msg = await message.answer(f"📥 Скачиваю изображение «{doc.file_name}»...")
        try:
            file = await bot.get_file(doc.file_id)
            image_bytes = await bot.download_file(file.file_path)
            
            caption = message.caption or ""
            
            # Создаем структуру, совместимую с функцией обработки фото
            photo_data = [{'file_id': doc.file_id, 'caption': caption}]
            
            # Если настройки обработки фото стоят на 'text' (OCR), используем OCR
            # Если 'image' (Vision) - используем Vision
            if settings.get('photo_processing') == 'image':
                # Для Vision нужно base64
                image_base64 = base64.b64encode(image_bytes.read()).decode('utf-8')
                await processing_msg.edit_text(f"🔄 Анализирую изображение через {settings['model']}...")
                prompt = caption or "Что изображено на этом файле?"
                response_text = await send_image_to_api(settings, image_base64, prompt)
                await processing_msg.delete()
                await send_response(message.chat.id, response_text, settings)
            else:
                # OCR для файла-картинки
                text = await ocr_space_api(image_bytes, doc.file_name)
                await processing_msg.edit_text(f"🔄 Модель обрабатывает текст из файла...")
                full_prompt = f"Текст из файла {doc.file_name}:\n{text}\n\nЗапрос: {caption}"
                response_text = await send_to_chatgpt(settings, full_prompt)
                await processing_msg.delete()
                await send_response(message.chat.id, response_text, settings)
                
        except Exception as e:
            await processing_msg.edit_text(f"❌ Ошибка обработки файла-изображения: {str(e)}")
        return

    # 2. Обрабатываем текстовые файлы
    processing_msg = await message.answer(f"📥 Читаю файл «{doc.file_name}»...")
    
    if doc.file_size > 5 * 1024 * 1024: # Ограничение 5 МБ для текста
        await processing_msg.edit_text("❌ Файл слишком большой для текстового анализа (макс 5 МБ).")
        return

    try:
        file = await bot.get_file(doc.file_id)
        file_content = await bot.download_file(file.file_path)
        
        # Пытаемся декодировать как текст
        try:
            text_content = file_content.read().decode('utf-8')
        except UnicodeDecodeError:
            await processing_msg.edit_text("❌ Не удалось прочитать файл как текст (бинарный файл или неверная кодировка).")
            return

        # Формируем промпт
        user_query = message.caption or "Проанализируй этот файл."
        prompt = (
            f"Пользователь прислал файл: {doc.file_name}\n"
            f"Содержимое файла:\n"
            f"```\n{text_content}\n```\n\n"
            f"Запрос пользователя к этому файлу: {user_query}"
        )

        await processing_msg.edit_text(f"🔄 Отправляю содержимое файла в {settings['model']}...")
        response_text = await send_to_chatgpt(settings, prompt)
        
        await processing_msg.delete()
        await send_response(message.chat.id, response_text, settings)

    except Exception as e:
        logger.error(f"Error handling file: {e}")
        await processing_msg.edit_text(f"❌ Ошибка при чтении файла: {str(e)}")


# ————————————————————————————————————————————————————————
# Photo Processing
# ————————————————————————————————————————————————————————

@router.message(F.photo)
async def handle_photos(message: types.Message):
    user_id = message.from_user.id
    settings = user_storage.get_active_profile_settings(user_id)
    if not settings.get('baseurl'):
        await message.answer("❌ Сначала настройте бот: /settings")
        return
    
    ask_before_send = settings.get('ask_before_send_photos', True)
    
    if user_id not in photo_sessions:
        photo_sessions[user_id] = {'photos': [], 'last_message_id': None}
    
    photo_sessions[user_id]['photos'].append({
        'file_id': message.photo[-1].file_id,
        'caption': message.caption or ""
    })
    
    if not ask_before_send:
        await asyncio.sleep(0.5)
        await asyncio.sleep(0.5)
        photo_data = photo_sessions[user_id]['photos'].copy()
        photo_sessions.pop(user_id, None)
        await process_photo_session(message.chat.id, user_id, photo_data, settings)
        return
    
    total_photos = len(photo_sessions[user_id]['photos'])
    confirmation_text = f"📸 В сумме {total_photos} фото, начать обработку?"
    keyboard = get_album_confirmation_keyboard()
    
    if photo_sessions[user_id]['last_message_id']:
        try:
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=photo_sessions[user_id]['last_message_id'],
                text=confirmation_text,
                reply_markup=keyboard
            )
        except Exception:
            new_message = await message.answer(confirmation_text, reply_markup=keyboard)
            photo_sessions[user_id]['last_message_id'] = new_message.message_id
    else:
        new_message = await message.answer(confirmation_text, reply_markup=keyboard)
        photo_sessions[user_id]['last_message_id'] = new_message.message_id


@router.callback_query(F.data == "album_send")
async def handle_album_send(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in photo_sessions or not photo_sessions[user_id]['photos']:
        await callback.answer("❌ Нет фото")
        return
    await callback.message.delete()
    settings = user_storage.get_active_profile_settings(user_id)
    photo_data = photo_sessions[user_id]['photos']
    photo_sessions.pop(user_id, None)
    await process_photo_session(callback.message.chat.id, user_id, photo_data, settings)


@router.callback_query(F.data == "album_cancel")
async def handle_album_cancel(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if user_id in photo_sessions: photo_sessions.pop(user_id, None)
    await callback.message.edit_text("❌ Отменено")
    await callback.answer()


async def process_photo_session(chat_id: int, user_id: int, photo_data: List[dict], settings: dict):
    processing_msg = await bot.send_message(chat_id, f"🔄 Обрабатываю {len(photo_data)} фото...")
    try:
        if settings.get('photo_processing', 'text') == 'text':
            await process_photos_as_text(chat_id, user_id, photo_data, settings, processing_msg)
        else:
            await process_photos_as_image(chat_id, user_id, photo_data, settings, processing_msg)
    except Exception as e:
        logger.error(f"Error processing photos: {e}")
        await processing_msg.edit_text(f"❌ Ошибка: {str(e)}")


async def process_photos_as_text(chat_id: int, user_id: int, photo_data: List[dict], settings: dict, processing_msg: types.Message):
    all_texts = []
    for i, photo_info in enumerate(photo_data, 1):
        try:
            file = await bot.get_file(photo_info['file_id'])
            image_bytes = await bot.download_file(file.file_path)
            text = await ocr_space_api(image_bytes, f'image_{i}.jpg')
            if text.strip():
                caption = f" (подпись: {photo_info['caption']})" if photo_info['caption'] else ""
                all_texts.append(f"--- Текст с фото {i}{caption} ---\n{text}")
        except Exception:
            continue
    
    if not all_texts:
        await processing_msg.edit_text("❌ Не удалось распознать текст")
        return
    
    combined_text = "\n".join(all_texts)
    await processing_msg.edit_text(f"🔄 Модель {settings['model']} генерирует ответ...")
    response_text = await send_to_chatgpt(settings, combined_text)
    await processing_msg.delete()
    await send_response(chat_id, response_text, settings)


async def process_photos_as_image(chat_id: int, user_id: int, photo_data: List[dict], settings: dict, processing_msg: types.Message):
    if not photo_data: return
    first_photo = photo_data[0]
    try:
        file = await bot.get_file(first_photo['file_id'])
        image_bytes = await bot.download_file(file.file_path)
        image_base64 = base64.b64encode(image_bytes.read()).decode('utf-8')
        prompt = first_photo['caption'] or "Что изображено на фото?"
        await processing_msg.edit_text(f"🔄 Анализ изображения ({settings['model']})...")
        response_text = await send_image_to_api(settings, image_base64, prompt)
        await processing_msg.delete()
        await send_response(chat_id, response_text, settings)
    except Exception as e:
        await processing_msg.edit_text(f"❌ Ошибка: {str(e)}")


async def send_response(chat_id: int, response_text: str, settings: dict):
    if settings.get('split_response', False) and len(response_text) > 390:
        chunks = [response_text[i:i+390] for i in range(0, len(response_text), 390)]
        for i, chunk in enumerate(chunks, 1):
            await bot.send_message(chat_id, f"{i}/{len(chunks)}\n{chunk}")
            await asyncio.sleep(4)
    else:
        await bot.send_message(chat_id, response_text)


async def ocr_space_api(image_bytes: bytes, filename: str) -> str:
    # Важно: bytes object должен быть в начале (seek 0) если он был прочитан
    if hasattr(image_bytes, 'seek'): image_bytes.seek(0)
    
    url = "https://api.ocr.space/parse/image"
    form_data = aiohttp.FormData()
    form_data.add_field('file', image_bytes, filename=filename, content_type='image/jpeg')
    form_data.add_field('apikey', OCR_API_TOKEN)
    form_data.add_field('language', 'rus')
    form_data.add_field('isOverlayRequired', 'false')
    form_data.add_field('OCREngine', '2')
    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=form_data) as response:
            result = await response.json()
            if result.get('IsErroredOnProcessing', False): raise Exception(result.get('ErrorMessage', ['Error'])[0])
            parsed = result.get('ParsedResults', [])
            return parsed[0].get('ParsedText', '') if parsed else ""


# ————————————————————————————————————————————————————————
# Text Handling
# ————————————————————————————————————————————————————————

@router.message(F.text)
async def handle_text_message(message: types.Message):
    if message.text.startswith('/'): return
    user_id = message.from_user.id
    settings = user_storage.get_active_profile_settings(user_id)
    if not settings.get('baseurl'):
        await message.answer("❌ Сначала настройте бот: /settings")
        return
    processing_msg = await message.answer(f"🔄 Модель {settings['model']} генерирует ответ...")
    try:
        response_text = await send_to_chatgpt(settings, message.text)
        await processing_msg.delete()
        await send_response(message.chat.id, response_text, settings)
    except Exception as e:
        await processing_msg.delete()
        await message.answer(f"❌ Ошибка: {str(e)}")



@router.message(F.video)
async def handle_video_message(message: types.Message):
    """
    Принимает видео и отправляет его напрямую в API в виде base64.
    Caption учитывается как промпт. Системный промпт и настройки профиля применяются.
    """
    user_id = message.from_user.id
    settings = user_storage.get_active_profile_settings(user_id)

    if not settings.get('baseurl'):
        await message.answer("❌ Сначала настройте бот: /settings")
        return

    video = message.video
    file_size_mb = (video.file_size or 0) / (1024 * 1024)

    if file_size_mb > 20:
        await message.answer(
            f"❌ Видео слишком большое ({file_size_mb:.1f} МБ). Максимум — 20 МБ."
        )
        return

    processing_msg = await message.answer(f"🎬 Скачиваю видео ({file_size_mb:.1f} МБ)...")

    try:
        file = await bot.get_file(video.file_id)
        file_io = io.BytesIO()
        await bot.download_file(file.file_path, file_io)
        file_io.seek(0)

        await processing_msg.edit_text("🔄 Кодирую видео и отправляю в модель...")

        video_base64 = base64.b64encode(file_io.read()).decode('utf-8')
        mime_type = video.mime_type or "video/mp4"
        user_caption = message.caption or "Опиши, что происходит в этом видео."

        await processing_msg.edit_text(f"🤖 Модель {settings['model']} анализирует видео...")

        response_text = await send_video_to_api(settings, video_base64, user_caption, mime_type)

        await processing_msg.delete()
        await send_response(message.chat.id, response_text, settings)

    except Exception as e:
        logger.error(f"Video error: {e}")
        await processing_msg.edit_text(f"❌ Ошибка обработки видео: {str(e)}")
# -----------------------------------------------

# ————————————————————————————————————————————————————————
# API Clients
# ————————————————————————————————————————————————————————

def remove_think_tags(text: str) -> str:
    import re
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'<think>.*', '', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'\n\s*\n', '\n', text)
    return text.strip() or "Ответ был удален (только <think>)"


def get_thinking_config(settings: dict) -> dict | None:
    """
    Возвращает thinking_config для API или None если выключено.
    - Gemini 3+: {"thinking_level": "low/medium/high"}
    - Остальные (Gemini 2.5 и др.): {"thinking_budget": N}
    """
    level = settings.get('thinking_budget', 'off')
    if level == 'off':
        return None

    model = settings.get('model', '').lower()
    is_gemini3 = 'gemini-3' in model or 'gemini3' in model

    if is_gemini3:
        # Gemini 3 использует строковый thinking_level
        return {"thinking_level": level}  # level уже = "low"/"medium"/"high"
    else:
        # Gemini 2.5 и другие используют числовой thinking_budget
        budget_map = {
            'low': 1024,
            'medium': 8192,
            'high': 24576,
        }
        tokens = budget_map.get(level)
        return {"thinking_budget": tokens} if tokens else None


async def send_to_perplexity(settings: dict, message: str) -> str:
    url = "https://api.perplexity.ai/chat/completions"
    sys_prompt = f"{settings.get('model_prompt', '')}. Запрещается использовать markdown и прочую разметку, не добавляй эмодзи."
    if settings.get('response_format') == 'short': sys_prompt += " No explanations."
    
    user_id = settings.get('_user_id')
    if user_id and user_id not in user_sessions:
        user_sessions[user_id] = []
        
    messages_payload = [{"role": "system", "content": sys_prompt}]
    if user_id:
        messages_payload.extend(user_sessions[user_id])
    messages_payload.append({"role": "user", "content": message})
    
    data = {
        "model": settings['model'],
        "messages": messages_payload,
        "max_tokens": settings.get('max_tokens', 1200),
        "temperature": 0.3
    }
    headers = {"Authorization": f"Bearer {settings['apikey']}", "Content-Type": "application/json"}
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=data) as response:
            if response.status != 200: raise Exception(await response.text())
            result = await response.json()
            content = remove_think_tags(result['choices'][0]['message']['content'])
            
            if user_id:
                user_sessions[user_id].append({"role": "user", "content": message})
                user_sessions[user_id].append({"role": "assistant", "content": content})
                if len(user_sessions[user_id]) > 40:
                    user_sessions[user_id] = user_sessions[user_id][-40:]
                    
            return content


# ————————————————————————————————————————————————————————
# API Clients (Обновленные)
# ————————————————————————————————————————————————————————

async def send_to_chatgpt(settings: dict, message: str) -> str:
    # Если это Perplexity, перенаправляем (функцию send_to_perplexity можно оставить как есть или обновить аналогично)
    if "perplexity.ai" in settings['baseurl']: 
        return await send_to_perplexity(settings, message)
    
    url = f"{settings['baseurl']}/chat/completions"
    
    # Логика формирования системного промпта
    format_type = settings.get('response_format', 'normal')
    base_sys = settings.get('model_prompt', '')
    
    # Жесткие инструкции в зависимости от режима
    if format_type == 'short':
        sys_instruction = "Запрещается использовать markdown и прочую разметку, не добавляй эмодзи. Твоя задача — отвечать МАКСИМАЛЬНО кратко. Исключи любые вступления, пояснения и вежливость. Сразу суть и сухие факты."
    elif format_type == 'medium':
        sys_instruction = "Запрещается использовать markdown и прочую разметку, не добавляй эмодзи. Отвечай лаконично и по делу. Избегай длинных философских рассуждений. Только конкретика. Слишком длинные ответы не приветсвуются."
    else:
        sys_instruction = "Запрещается использовать markdown и прочую разметку, не добавляй эмодзи."

    # Объединяем пользовательский промпт и настройки режима
    full_sys_prompt = f"{sys_instruction} {base_sys}".strip()
    
    # Для режима 'short' можно дополнительно урезать max_tokens, чтобы физически ограничить модель
    # Но лучше полагаться на промпт, чтобы не обрывать слова.
    
    user_id = settings.get('_user_id')
    if user_id and user_id not in user_sessions:
        user_sessions[user_id] = []
        
    messages_payload = [{"role": "system", "content": full_sys_prompt}]
    if user_id:
        messages_payload.extend(user_sessions[user_id])
    messages_payload.append({"role": "user", "content": message})
    
    data = {
        "model": settings['model'],
        "messages": messages_payload,
        "max_tokens": settings.get('max_tokens', 2000),
        "temperature": 0.5 if format_type == 'short' else 0.7 # Понижаем температуру для коротких ответов (меньше креатива, больше четкости)
    }
    thinking_cfg = get_thinking_config(settings)
    if thinking_cfg:
        data["thinking_config"] = thinking_cfg
    
    headers = {
        "Content-Type": "application/json", 
        "Authorization": f"Bearer {settings['apikey']}"
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=data) as response:
            if response.status != 200: 
                error_text = await response.text()
                raise Exception(f"API Error {response.status}: {error_text}")
            
            result = await response.json()
            content = result['choices'][0]['message']['content']
            clean_content = remove_think_tags(content)
            
            if user_id:
                user_sessions[user_id].append({"role": "user", "content": message})
                user_sessions[user_id].append({"role": "assistant", "content": clean_content})
                if len(user_sessions[user_id]) > 40:
                    user_sessions[user_id] = user_sessions[user_id][-40:]
            
            return clean_content


async def send_image_to_api(settings: dict, image_base64: str, prompt: str) -> str:
    if "perplexity.ai" in settings['baseurl']: 
        return await send_to_perplexity(settings, prompt)
    
    url = f"{settings['baseurl']}/chat/completions"
    
    format_type = settings.get('response_format', 'normal')
    base_sys = settings.get('model_prompt', '')
    
    # Те же жесткие инструкции для картинок
    if format_type == 'short':
        sys_instruction = "Проанализируй изображение и ответь ОЧЕНЬ кратко. Только суть. Запрещается использовать markdown и прочую разметку, не добавляй эмодзи."
    elif format_type == 'medium':
        sys_instruction = "Отвечай по делу, без лишней воды. Запрещается использовать markdown и прочую разметку, не добавляй эмодзи."
    else:
        sys_instruction = "Запрещается использовать markdown и прочую разметку, не добавляй эмодзи."

    full_sys_prompt = f"{sys_instruction} {base_sys}".strip()

    user_id = settings.get('_user_id')
    if user_id and user_id not in user_sessions:
        user_sessions[user_id] = []
        
    messages_payload = [{"role": "system", "content": full_sys_prompt}]
    if user_id:
        messages_payload.extend(user_sessions[user_id])
    messages_payload.append({
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}}
        ]
    })

    data = {
        "model": settings['model'],
        "messages": messages_payload,
        "max_tokens": settings.get('max_tokens', 2000),
        "temperature": 0.5 if format_type == 'short' else 0.7
    }
    thinking_cfg = get_thinking_config(settings)
    if thinking_cfg:
        data["thinking_config"] = thinking_cfg
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {settings['apikey']}"}
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=data) as response:
            if response.status != 200: 
                error_text = await response.text()
                raise Exception(f"API Error {response.status}: {error_text}")
            
            result = await response.json()
            content = result['choices'][0]['message']['content']
            clean_content = remove_think_tags(content)
            
            if user_id:
                user_sessions[user_id].append({"role": "user", "content": f"[User sent an image]: {prompt}"})
                user_sessions[user_id].append({"role": "assistant", "content": clean_content})
                if len(user_sessions[user_id]) > 40:
                    user_sessions[user_id] = user_sessions[user_id][-40:]
                    
            return clean_content
        
async def send_video_to_api(settings: dict, video_base64: str, prompt: str, mime_type: str = "video/mp4") -> str:
    """
    Отправляет видео в API как base64. Системный промпт из настроек профиля (model_prompt + format).
    Caption передаётся как пользовательский промпт.
    Работает с моделями, поддерживающими video input (например, Gemini через OpenAI-endpoint).
    """
    if "perplexity.ai" in settings['baseurl']:
        return await send_to_perplexity(settings, prompt)

    url = f"{settings['baseurl']}/chat/completions"
    format_type = settings.get('response_format', 'normal')
    base_sys = settings.get('model_prompt', '')

    if format_type == 'short':
        sys_instruction = "Проанализируй видео и ответь ОЧЕНЬ кратко. Только суть. Запрещается использовать markdown и прочую разметку, не добавляй эмодзи."
    elif format_type == 'medium':
        sys_instruction = "Отвечай по делу, без лишней воды. Запрещается использовать markdown и прочую разметку, не добавляй эмодзи."
    else:
        sys_instruction = "Запрещается использовать markdown и прочую разметку, не добавляй эмодзи."

    full_sys_prompt = f"{sys_instruction} {base_sys}".strip()

    user_id = settings.get('_user_id')
    if user_id and user_id not in user_sessions:
        user_sessions[user_id] = []
        
    messages_payload = [{"role": "system", "content": full_sys_prompt}]
    if user_id:
        messages_payload.extend(user_sessions[user_id])
    messages_payload.append({
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{video_base64}"}}
        ]
    })

    data = {
        "model": settings['model'],
        "messages": messages_payload,
        "max_tokens": settings.get('max_tokens', 2000),
        "temperature": 0.5 if format_type == 'short' else 0.7
    }
    thinking_cfg = get_thinking_config(settings)
    if thinking_cfg:
        data["thinking_config"] = thinking_cfg
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {settings['apikey']}"}

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=data) as response:
            if response.status != 200:
                error_text = await response.text()
                raise Exception(f"API Error {response.status}: {error_text}")
            result = await response.json()
            content = result['choices'][0]['message']['content']
            clean_content = remove_think_tags(content)
            
            if user_id:
                user_sessions[user_id].append({"role": "user", "content": f"[User sent a video]: {prompt}"})
                user_sessions[user_id].append({"role": "assistant", "content": clean_content})
                if len(user_sessions[user_id]) > 40:
                    user_sessions[user_id] = user_sessions[user_id][-40:]
                    
            return clean_content


async def main():
    logger.info("Бот запущен")
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
