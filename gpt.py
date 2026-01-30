import asyncio
import logging
import json
import os
import base64
import aiohttp
from aiogram import Bot, Dispatcher, types, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from typing import Dict, Any, List

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Токены API
API_TOKEN = "ss"
OCR_API_TOKEN = "sss"

# Инициализация
bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

# Файл для хранения данных пользователей
USER_DATA_FILE = "users.json"

# Словарь для накопления фото из альбомов
photo_albums = {}

# Словарь для сессий пользователей (новая функция)
user_sessions = {}


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
                        'model_prompt': old_settings.get('model_prompt', '')  # Новое поле
                    }
                },
                'active_profile': 'default',
                'response_format': old_settings.get('response_format', 'normal'),
                'split_response': old_settings.get('split_response', False),
                'ask_before_send_photos': old_settings.get('ask_before_send_photos', True)
            }
        
        # Миграция: переносим ask_before_send_photos из профилей в общие настройки
        if 'ask_before_send_photos' not in user_data and 'profiles' in user_data:
            # Ищем значение в любом профиле или используем значение по умолчанию
            ask_photos_value = True
            for profile_name, profile_data in user_data['profiles'].items():
                if 'ask_before_send_photos' in profile_data:
                    ask_photos_value = profile_data['ask_before_send_photos']
                    break
            user_data['ask_before_send_photos'] = ask_photos_value
            
            # Удаляем из всех профилей
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
            '_all_profiles': list(user_data['profiles'].keys())
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
        """Устанавливает общий параметр (response_format, split_response, ask_before_send_photos)"""
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
            'model_prompt': ''  # Новое поле
        }
        self.set_user_data(user_id, user_data)

    def delete_profile(self, user_id: int, profile_name: str):
        """Удаляет профиль (если не активный или не последний)"""
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


# Состояния для редактирования профилей
class ProfileStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_baseurl = State()
    waiting_for_apikey = State()
    waiting_for_model = State()
    waiting_for_max_tokens = State()
    waiting_for_rename = State()
    waiting_for_delete_confirm = State()
    waiting_for_model_prompt = State()  # Новое состояние


# Состояния для настроек
class SettingsStates(StatesGroup):
    baseurl = State()
    apikey = State()
    model = State()
    max_tokens = State()

# Состояние для переноса профилей в ВК
class TransferStates(StatesGroup):
    waiting_for_vk_id = State()


# ————————————————————————————————————————————————————————
# Утилиты для интерфейса
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
        f"💬 Промпт модели: {model_prompt_text}\n"
        f"Выберите параметр для изменения:"
    )
    return settings_text


def get_profile_keyboard(settings: Dict[str, Any]) -> types.InlineKeyboardMarkup:
    response_format = settings.get('response_format', 'normal')
    split_response = settings.get('split_response', False)
    photo_processing = settings.get('photo_processing', 'text')
    
    format_buttons = [
        [
            types.InlineKeyboardButton(
                text=f"{'✅' if response_format == 'normal' else '○'} Обычный", 
                callback_data="format_normal"
            ),
            types.InlineKeyboardButton(
                text=f"{'✅' if response_format == 'medium' else '○'} Средний", 
                callback_data="format_medium"
            ),
            types.InlineKeyboardButton(
                text=f"{'✅' if response_format == 'short' else '○'} Короткий", 
                callback_data="format_short"
            )
        ]
    ]
    
    split_buttons = [
        [
            types.InlineKeyboardButton(
                text=f"{'✅' if split_response else '○'} Разделять", 
                callback_data="split_true"
            ),
            types.InlineKeyboardButton(
                text=f"{'✅' if not split_response else '○'} Не разделять", 
                callback_data="split_false"
            )
        ]
    ]
    
    photo_buttons = [
        [
            types.InlineKeyboardButton(
                text=f"{'✅' if photo_processing == 'text' else '○'} Как текст (OCR)", 
                callback_data="photo_text"
            ),
            types.InlineKeyboardButton(
                text=f"{'✅' if photo_processing == 'image' else '○'} Как фото (Vision)", 
                callback_data="photo_image"
            )
        ]
    ]
    
    ask_photo_buttons = [
        [
            types.InlineKeyboardButton(
                text=f"{'✅' if settings.get('ask_before_send_photos', True) else '○'} Спрашивать", 
                callback_data="ask_photo_true"
            ),
            types.InlineKeyboardButton(
                text=f"{'✅' if not settings.get('ask_before_send_photos', True) else '○'} Отправлять сразу", 
                callback_data="ask_photo_false"
            )
        ]
    ]
    
    profile_buttons = [
        [types.InlineKeyboardButton(text="👤 Управление профилями", callback_data="manage_profiles")],
        [types.InlineKeyboardButton(text="💬 Изменить промпт модели", callback_data="change_model_prompt")],
        [types.InlineKeyboardButton(text="🗑️ Очистить промпт модели", callback_data="clear_model_prompt")]
    ]
    
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🔗 Изменить Base URL", callback_data="change_baseurl")],
        [types.InlineKeyboardButton(text="🔑 Изменить API ключ", callback_data="change_apikey")],
        [types.InlineKeyboardButton(text="🤖 Изменить модель", callback_data="change_model")],
        [types.InlineKeyboardButton(text="📊 Изменить макс. токены", callback_data="change_tokens")],
        [types.InlineKeyboardButton(text="🖼️ Изменить обработку фото", callback_data="change_photo_processing")],
        [types.InlineKeyboardButton(text="📸 Подтверждение отправки фото", callback_data="show_ask_photo")],
        *ask_photo_buttons,
        [types.InlineKeyboardButton(text="📝 Формат ответа", callback_data="show_formats")],
        *format_buttons,
        [types.InlineKeyboardButton(text="📋 Разделение ответа", callback_data="show_split")],
        *split_buttons,
        *profile_buttons,
        [types.InlineKeyboardButton(text="❌ Закрыть настройки", callback_data="close_settings")]
    ])
    return keyboard


def get_profiles_manager_keyboard(profiles: List[str], active: str) -> types.InlineKeyboardMarkup:
    buttons = []
    for name in profiles:
        mark = "✅ " if name == active else "○ "
        row_buttons = [
            types.InlineKeyboardButton(
                text=f"{mark}{name}",
                callback_data=f"switch_profile_{name}"
            ),
            types.InlineKeyboardButton(
                text="✏️",
                callback_data=f"rename_profile_{name}"
            ),
            types.InlineKeyboardButton(
                text="🗑️",
                callback_data=f"delete_profile_{name}"
            )
        ]
        buttons.append(row_buttons)
    
    buttons.append([types.InlineKeyboardButton(text="➕ Создать профиль", callback_data="create_profile")])
    buttons.append([types.InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_settings")])
    return types.InlineKeyboardMarkup(inline_keyboard=buttons)


def get_album_confirmation_keyboard() -> types.InlineKeyboardMarkup:
    """Клавиатура для подтверждения отправки альбома"""
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(text="✅ Отправить", callback_data="album_send"),
            types.InlineKeyboardButton(text="❌ Прекратить", callback_data="album_cancel")
        ]
    ])


# ————————————————————————————————————————————————————————
# Обработчики команд и кнопок
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
        "/show_settings — Показать текущие настройки\n"
        "/delete_settings — Удалить мои настройки\n"
        "Просто отправьте текст или фото — бот автоматически ответит!\nБот в ВК - https://vk.ru/club235624714",
        reply_markup=keyboard
    )


@router.callback_query(F.data == "transfer_to_vk")
async def transfer_to_vk_handler(callback: types.CallbackQuery, state: FSMContext):
    """Обработчик начала переноса профилей в ВК"""
    await callback.message.answer(
        "📤 Перенос профилей в ВК бота\n\n"
        "Введите ваш ID пользователя ВКонтакте (число).\n\n"
        "Чтобы узнать свой ID:\n"
        "Перейдите по ссылке: https://vk.com/account?open_page=personal\n"
        "Там отображается ваш ID пользователя.\n\n"
        "Или:\n"
        "1. Откройте свой профиль ВК\n"
        "2. Скопируйте число из URL (например, vk.com/id123456789)\n"
        "3. Отправьте это число боту"
    )
    await state.set_state(TransferStates.waiting_for_vk_id)
    await callback.answer()


@router.message(TransferStates.waiting_for_vk_id)
async def process_vk_id(message: types.Message, state: FSMContext):
    """Обработка ID ВК и перенос профилей"""
    try:
        vk_id = int(message.text.strip())
        if vk_id <= 0:
            await message.answer("❌ ID должен быть положительным числом. Перенос отменен.")
            await state.clear()
            return
    except ValueError:
        await message.answer("❌ Пожалуйста, введите корректный ID (число). Перенос отменен.")
        await state.clear()
        return
    
    user_id = message.from_user.id
    user_data = user_storage.get_user_data(user_id)
    
    if not user_data or not user_data.get('profiles'):
        await message.answer("❌ У вас нет профилей для переноса. Сначала создайте профили через /settings")
        await state.clear()
        return
    
    # Загружаем данные ВК бота
    vk_storage = UserStorage("users_vk.json")
    vk_users = vk_storage.load_users()
    
    # Переносим профили
    vk_users[str(vk_id)] = user_data
    
    # Сохраняем в файл ВК бота
    vk_storage.save_users(vk_users)
    
    profiles_count = len(user_data.get('profiles', {}))
    await message.answer(
        f"✅ Профили успешно перенесены в ВК бота!\n\n"
        f"📊 Перенесено профилей: {profiles_count}\n"
        f"🆔 Ваш ID ВК: {vk_id}\n\n"
        f"Теперь вы можете использовать эти профили в ВК боте. Ссылка на него: https://vk.ru/club235624714"
    )
    await state.clear()


@router.message(Command("settings"))
async def cmd_settings(message: types.Message):
    settings = user_storage.get_active_profile_settings(message.from_user.id)
    await message.answer(
        get_profile_settings_text(settings),
        reply_markup=get_profile_keyboard(settings),
        parse_mode='HTML'
    )


@router.callback_query(F.data == "manage_profiles")
async def manage_profiles(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    settings = user_storage.get_active_profile_settings(user_id)
    await callback.message.edit_text(
        "👤 <b>Управление профилями</b>\nВыберите профиль для активации или создайте новый:",
        reply_markup=get_profiles_manager_keyboard(settings['_all_profiles'], settings['_active_profile_name']),
        parse_mode='HTML'
    )
    await callback.answer()


@router.callback_query(F.data == "back_to_settings")
async def back_to_settings(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    settings = user_storage.get_active_profile_settings(user_id)
    await callback.message.edit_text(
        get_profile_settings_text(settings),
        reply_markup=get_profile_keyboard(settings),
        parse_mode='HTML'
    )
    await callback.answer()


@router.callback_query(F.data.startswith("switch_profile_"))
async def switch_profile_handler(callback: types.CallbackQuery):
    profile_name = callback.data.replace("switch_profile_", "")
    user_id = callback.from_user.id
    try:
        user_storage.switch_profile(user_id, profile_name)
        settings = user_storage.get_active_profile_settings(user_id)
        await callback.message.edit_text(
            get_profile_settings_text(settings),
            reply_markup=get_profile_keyboard(settings),
            parse_mode='HTML'
        )
    except Exception as e:
        await callback.answer(f"Ошибка: {str(e)}", show_alert=True)
    else:
        await callback.answer("✅ Профиль переключён")


@router.callback_query(F.data.startswith("delete_profile_"))
async def delete_profile_handler(callback: types.CallbackQuery, state: FSMContext):
    profile_name = callback.data.replace("delete_profile_", "")
    user_id = callback.from_user.id
    
    # Проверяем, можно ли удалить профиль
    try:
        user_storage.delete_profile(user_id, profile_name)
        # Если удаление успешно, обновляем список профилей
        settings = user_storage.get_active_profile_settings(user_id)
        await callback.message.edit_text(
            "👤 <b>Управление профилями</b>\nВыберите профиль для активации или создайте новый:",
            reply_markup=get_profiles_manager_keyboard(settings['_all_profiles'], settings['_active_profile_name']),
            parse_mode='HTML'
        )
        await callback.answer(f"✅ Профиль «{profile_name}» удалён")
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)


@router.callback_query(F.data.startswith("rename_profile_"))
async def rename_profile_handler(callback: types.CallbackQuery, state: FSMContext):
    old_name = callback.data.replace("rename_profile_", "")
    user_id = callback.from_user.id
    
    await callback.message.answer(f"Введите новое имя для профиля «{old_name}»:")
    await state.set_state(ProfileStates.waiting_for_rename)
    await state.update_data(old_name=old_name)
    await callback.answer()


@router.message(ProfileStates.waiting_for_rename)
async def process_profile_rename(message: types.Message, state: FSMContext):
    new_name = message.text.strip()
    if not new_name:
        await message.answer("❌ Имя не может быть пустым. Попробуйте снова:")
        return
    if len(new_name) > 32:
        await message.answer("❌ Имя слишком длинное (макс. 32 символа). Попробуйте снова:")
        return
    
    user_data = await state.get_data()
    old_name = user_data['old_name']
    user_id = message.from_user.id
    
    try:
        user_storage.rename_profile(user_id, old_name, new_name)
        settings = user_storage.get_active_profile_settings(user_id)
        await message.answer(
            f"✅ Профиль «{old_name}» переименован в «{new_name}»",
            reply_markup=get_profiles_manager_keyboard(settings['_all_profiles'], settings['_active_profile_name']),
            parse_mode='HTML'
        )
        await state.clear()
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}\nПопробуйте другое имя:")


@router.callback_query(F.data == "create_profile")
async def create_profile_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите имя нового профиля:")
    await state.set_state(ProfileStates.waiting_for_name)
    await callback.answer()


@router.message(ProfileStates.waiting_for_name)
async def create_profile_name(message: types.Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("❌ Имя не может быть пустым. Попробуйте снова:")
        return
    if len(name) > 32:
        await message.answer("❌ Имя слишком длинное (макс. 32 символа). Попробуйте снова:")
        return
    user_id = message.from_user.id
    try:
        user_storage.create_profile(user_id, name)
        user_storage.switch_profile(user_id, name)
        settings = user_storage.get_active_profile_settings(user_id)
        await message.answer(
            f"✅ Профиль «{name}» создан и активирован.\nТеперь настройте его параметры:",
            reply_markup=get_profile_keyboard(settings),
            parse_mode='HTML'
        )
        await state.clear()
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}\nПопробуйте другое имя:")
        return


# ————————————————————————————————————————————————————————
# Обработка изменений настроек (включая промпт модели)
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
        await callback.message.answer(
            "Введите максимальное количество токенов (например: 2000):\n"
            "Рекомендации:\n"
            "• 500-1000 — короткие ответы\n"
            "• 1000-2000 — средние ответы\n"
            "• 2000-4000 — длинные ответы\n"
            "• 4000+ — очень длинные ответы"
        )
        await state.set_state(SettingsStates.max_tokens)
    elif action == "change_photo_processing":
        settings = user_storage.get_active_profile_settings(callback.from_user.id)
        photo_processing = settings.get('photo_processing', 'text')
        
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=f"{'✅' if photo_processing == 'text' else '○'} Как текст (OCR)", 
                    callback_data="photo_text"
                ),
                types.InlineKeyboardButton(
                    text=f"{'✅' if photo_processing == 'image' else '○'} Как фото (Vision)", 
                    callback_data="photo_image"
                )
            ],
            [types.InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_settings")]
        ])
        
        await callback.message.edit_text(
            "🖼️ <b>Обработка фото</b>\n\n"
            "📝 <b>Как текст (OCR)</b> - распознает текст с изображения и отправляет его модели\n"
            "🖼️ <b>Как фото (Vision)</b> - отправляет само изображение в модель (требует поддержки vision)",
            reply_markup=keyboard,
            parse_mode='HTML'
        )
    elif action == "change_model_prompt":
        await callback.message.answer(
            "Введите промпт для модели. Этот текст будет добавляться перед каждым запросом пользователя:\n\n"
            "Например:\n"
            "• «Отвечай как опытный преподаватель»\n"
            "• «Объясняй простыми словами»\n"
            "• «Отвечай кратко и по делу»\n\n"
            "Отправьте текст промпта или /cancel для отмены:"
        )
        await state.set_state(ProfileStates.waiting_for_model_prompt)
    await callback.answer()


@router.callback_query(F.data == "clear_model_prompt")
async def clear_model_prompt(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    user_storage.set_profile_setting(user_id, 'model_prompt', '')
    settings = user_storage.get_active_profile_settings(user_id)
    await callback.message.edit_text(
        get_profile_settings_text(settings),
        reply_markup=get_profile_keyboard(settings),
        parse_mode='HTML'
    )
    await callback.answer("✅ Промпт модели очищен")


@router.message(ProfileStates.waiting_for_model_prompt)
async def process_model_prompt(message: types.Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Изменение промпта отменено")
        return
        
    model_prompt = message.text.strip()
    if not model_prompt:
        await message.answer("❌ Промпт не может быть пустым. Попробуйте снова или отправьте /cancel для отмены:")
        return
        
    user_storage.set_profile_setting(message.from_user.id, 'model_prompt', model_prompt)
    await state.clear()
    settings = user_storage.get_active_profile_settings(message.from_user.id)
    await message.answer(
        "✅ Промпт модели успешно сохранен!\n\n" +
        get_profile_settings_text(settings),
        reply_markup=get_profile_keyboard(settings),
        parse_mode='HTML'
    )


@router.message(SettingsStates.baseurl)
async def process_baseurl(message: types.Message, state: FSMContext):
    baseurl = message.text.strip()
    if not baseurl.startswith(('http://', 'https://')):
        await message.answer("❌ Неверный URL. Убедитесь, что URL начинается с http:// или https://")
        return
    user_storage.set_profile_setting(message.from_user.id, 'baseurl', baseurl)
    await state.clear()
    settings = user_storage.get_active_profile_settings(message.from_user.id)
    await message.answer(
        get_profile_settings_text(settings),
        reply_markup=get_profile_keyboard(settings),
        parse_mode='HTML'
    )


@router.message(SettingsStates.apikey)
async def process_apikey(message: types.Message, state: FSMContext):
    apikey = message.text.strip()
    if len(apikey) < 5:
        await message.answer("❌ API ключ слишком короткий")
        return
    user_storage.set_profile_setting(message.from_user.id, 'apikey', apikey)
    await state.clear()
    settings = user_storage.get_active_profile_settings(message.from_user.id)
    await message.answer(
        get_profile_settings_text(settings),
        reply_markup=get_profile_keyboard(settings),
        parse_mode='HTML'
    )


@router.message(SettingsStates.model)
async def process_model(message: types.Message, state: FSMContext):
    model = message.text.strip()
    if not model:
        await message.answer("❌ Название модели не может быть пустым")
        return
    user_storage.set_profile_setting(message.from_user.id, 'model', model)
    await state.clear()
    settings = user_storage.get_active_profile_settings(message.from_user.id)
    await message.answer(
        get_profile_settings_text(settings),
        reply_markup=get_profile_keyboard(settings),
        parse_mode='HTML'
    )


@router.message(SettingsStates.max_tokens)
async def process_max_tokens(message: types.Message, state: FSMContext):
    try:
        max_tokens = int(message.text.strip())
        if max_tokens < 100:
            await message.answer("❌ Количество токенов должно быть не менее 100")
            return
        if max_tokens > 60000:
            await message.answer("❌ Количество токенов должно быть не более 60000")
            return
    except ValueError:
        await message.answer("❌ Пожалуйста, введите целое число (например: 2000)")
        return
    user_storage.set_profile_setting(message.from_user.id, 'max_tokens', max_tokens)
    await state.clear()
    settings = user_storage.get_active_profile_settings(message.from_user.id)
    await message.answer(
        get_profile_settings_text(settings),
        reply_markup=get_profile_keyboard(settings),
        parse_mode='HTML'
    )


# Формат ответа и разделение — глобальные, обработка фото — в профиле
@router.callback_query(F.data.startswith("format_"))
async def handle_format_change(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    format_type = callback.data.replace("format_", "")
    user_storage.set_common_setting(user_id, 'response_format', format_type)
    settings = user_storage.get_active_profile_settings(user_id)
    await callback.message.edit_text(
        get_profile_settings_text(settings),
        reply_markup=get_profile_keyboard(settings),
        parse_mode='HTML'
    )
    await callback.answer()


@router.callback_query(F.data.startswith("split_"))
async def handle_split_change(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    split_value = callback.data == "split_true"
    user_storage.set_common_setting(user_id, 'split_response', split_value)
    settings = user_storage.get_active_profile_settings(user_id)
    await callback.message.edit_text(
        get_profile_settings_text(settings),
        reply_markup=get_profile_keyboard(settings),
        parse_mode='HTML'
    )
    await callback.answer()


@router.callback_query(F.data.startswith("photo_"))
async def handle_photo_processing_change(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    photo_mode = callback.data.replace("photo_", "")
    user_storage.set_profile_setting(user_id, 'photo_processing', photo_mode)
    settings = user_storage.get_active_profile_settings(user_id)
    await callback.message.edit_text(
        get_profile_settings_text(settings),
        reply_markup=get_profile_keyboard(settings),
        parse_mode='HTML'
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ask_photo_"))
async def handle_ask_photo_change(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    ask_value = callback.data == "ask_photo_true"
    user_storage.set_common_setting(user_id, 'ask_before_send_photos', ask_value)
    settings = user_storage.get_active_profile_settings(user_id)
    await callback.message.edit_text(
        get_profile_settings_text(settings),
        reply_markup=get_profile_keyboard(settings),
        parse_mode='HTML'
    )
    await callback.answer()


@router.callback_query(F.data == "show_ask_photo")
async def show_ask_photo_settings(callback: types.CallbackQuery):
    settings = user_storage.get_active_profile_settings(callback.from_user.id)
    await callback.message.edit_text(
        get_profile_settings_text(settings),
        reply_markup=get_profile_keyboard(settings),
        parse_mode='HTML'
    )
    await callback.answer()


@router.callback_query(F.data == "show_formats")
@router.callback_query(F.data == "show_split")
async def refresh_settings(callback: types.CallbackQuery):
    settings = user_storage.get_active_profile_settings(callback.from_user.id)
    await callback.message.edit_text(
        get_profile_settings_text(settings),
        reply_markup=get_profile_keyboard(settings),
        parse_mode='HTML'
    )
    await callback.answer()


@router.callback_query(F.data == "close_settings")
async def handle_close_settings(callback: types.CallbackQuery):
    await callback.message.delete()
    await callback.answer()


# ————————————————————————————————————————————————————————
# Обработка фото и текста (с новой функцией сессий)
# ————————————————————————————————————————————————————————

@router.message(Command("show_settings"))
async def cmd_show_settings(message: types.Message):
    settings = user_storage.get_active_profile_settings(message.from_user.id)
    if not settings.get('baseurl'):
        await message.answer("❌ Настройки не найдены. Используйте /settings для настройки бота.")
        return
    await message.answer(get_profile_settings_text(settings), parse_mode='HTML')


@router.message(Command("delete_settings"))
async def cmd_delete_settings(message: types.Message):
    settings = user_storage.get_active_profile_settings(message.from_user.id)
    if not settings:
        await message.answer("❌ Настройки не найдены.")
        return
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(text="✅ Да, удалить", callback_data="confirm_delete"),
            types.InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_delete")
        ]
    ])
    await message.answer("⚠️ Вы уверены, что хотите удалить свои настройки?", reply_markup=keyboard)


@router.callback_query(F.data == "confirm_delete")
async def confirm_delete(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    user_data = user_storage.get_user_data(user_id)
    # Удаляем всё
    users = user_storage.load_users()
    users.pop(str(user_id), None)
    user_storage.save_users(users)
    await callback.message.edit_text("✅ Настройки успешно удалены!")
    await callback.answer()


@router.callback_query(F.data == "cancel_delete")
async def cancel_delete(callback: types.CallbackQuery):
    await callback.message.edit_text("❌ Удаление отменено.")
    await callback.answer()


@router.message(F.photo)
async def handle_photos(message: types.Message):
    user_id = message.from_user.id
    settings = user_storage.get_active_profile_settings(user_id)
    if not settings.get('baseurl'):
        await message.answer("❌ Сначала настройте бот с помощью команды /settings")
        return
    
    # Проверяем настройку: нужно ли спрашивать перед отправкой
    ask_before_send = settings.get('ask_before_send_photos', True)
    
    # Добавляем фото в сессию
    if user_id not in user_sessions:
        user_sessions[user_id] = {
            'photos': [],
            'last_message_id': None,
            'media_group_id': None
        }
    
    file_id = message.photo[-1].file_id
    caption = message.caption or ""
    
    user_sessions[user_id]['photos'].append({
        'file_id': file_id,
        'caption': caption
    })
    
    # Если настройка выключена (auto_send = True), отправляем сразу
    if not ask_before_send:
        # Небольшая задержка для сбора всех фото из альбома (если это альбом)
        await asyncio.sleep(0.5)
        
        # Проверяем, не пришло ли еще фото (для альбомов Telegram отправляет их почти одновременно)
        # Даем еще немного времени
        await asyncio.sleep(0.5)
        
        # Отправляем на обработку
        photo_data = user_sessions[user_id]['photos'].copy()
        user_sessions.pop(user_id, None)  # Очищаем сессию
        
        await process_photo_session(message.chat.id, user_id, photo_data, settings)
        return
    
    # Если настройка включена - показываем кнопки подтверждения (старое поведение)
    total_photos = len(user_sessions[user_id]['photos'])
    confirmation_text = f"📸 В сумме {total_photos} фото, начать обработку?"
    
    keyboard = get_album_confirmation_keyboard()
    
    if user_sessions[user_id]['last_message_id']:
        try:
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=user_sessions[user_id]['last_message_id'],
                text=confirmation_text,
                reply_markup=keyboard
            )
        except Exception:
            # Если не удалось редактировать, отправляем новое сообщение
            new_message = await message.answer(confirmation_text, reply_markup=keyboard)
            user_sessions[user_id]['last_message_id'] = new_message.message_id
    else:
        new_message = await message.answer(confirmation_text, reply_markup=keyboard)
        user_sessions[user_id]['last_message_id'] = new_message.message_id


@router.callback_query(F.data == "album_send")
async def handle_album_send(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    if user_id not in user_sessions or not user_sessions[user_id]['photos']:
        await callback.answer("❌ Нет фото для обработки")
        return
    
    # Удаляем сообщение с кнопками
    await callback.message.delete()
    
    # Обрабатываем фото
    settings = user_storage.get_active_profile_settings(user_id)
    photo_data = user_sessions[user_id]['photos']
    
    # Очищаем сессию
    user_sessions.pop(user_id, None)
    
    await process_photo_session(callback.message.chat.id, user_id, photo_data, settings)


@router.callback_query(F.data == "album_cancel")
async def handle_album_cancel(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    if user_id in user_sessions:
        user_sessions.pop(user_id, None)
    
    await callback.message.edit_text("❌ Обработка фото отменена")
    await callback.answer()


async def process_photo_session(chat_id: int, user_id: int, photo_data: List[dict], settings: dict):
    """Обрабатывает сессию с фото"""
    processing_msg = await bot.send_message(chat_id, f"🔄 Обрабатываю {len(photo_data)} фото...")
    
    try:
        photo_processing_mode = settings.get('photo_processing', 'text')
        
        if photo_processing_mode == 'text':
            # Режим OCR - обрабатываем все фото
            await process_photos_as_text(chat_id, user_id, photo_data, settings, processing_msg)
        else:
            # Режим Vision - обрабатываем только первое фото (ограничение API)
            await process_photos_as_image(chat_id, user_id, photo_data, settings, processing_msg)
            
    except Exception as e:
        logger.error(f"Ошибка при обработке сессии фото: {str(e)}", exc_info=True)
        await processing_msg.edit_text(f"❌ Ошибка при обработке фото: {str(e)}")


async def process_photos_as_text(chat_id: int, user_id: int, photo_data: List[dict], settings: dict, processing_msg: types.Message):
    """Обработка фото через OCR"""
    all_texts = []
    
    for i, photo_info in enumerate(photo_data, 1):
        try:
            file = await bot.get_file(photo_info['file_id'])
            image_bytes = await bot.download_file(file.file_path)
            text = await ocr_space_api(image_bytes, f'image_{i}.jpg')
            if text.strip():
                caption_text = f" (подпись: {photo_info['caption']})" if photo_info['caption'] else ""
                all_texts.append(f"--- Текст с изображения {i}{caption_text} ---\n{text}")
        except Exception as e:
            logger.error(f"Ошибка при обработке фото {i}: {str(e)}")
            continue
    
    if not all_texts:
        await processing_msg.edit_text("❌ Не удалось распознать текст ни с одного изображения")
        return
    
    combined_text = "\n".join(all_texts)
    await processing_msg.edit_text(f"🔄 Модель {settings['model']} генерирует ответ...")
    
    response_text = await send_to_chatgpt(settings, combined_text)
    await processing_msg.delete()
    await send_response(chat_id, response_text, settings, len(all_texts))


async def process_photos_as_image(chat_id: int, user_id: int, photo_data: List[dict], settings: dict, processing_msg: types.Message):
    """Обработка фото как изображений (только первое фото)"""
    if not photo_data:
        await processing_msg.edit_text("❌ Нет фото для обработки")
        return
    
    # Берем только первое фото (ограничение API)
    first_photo = photo_data[0]
    
    try:
        file = await bot.get_file(first_photo['file_id'])
        image_bytes = await bot.download_file(file.file_path)
        
        if not image_bytes:
            await processing_msg.edit_text("❌ Не удалось скачать изображение")
            return
        
        # Кодируем в base64
        image_base64 = base64.b64encode(image_bytes.read()).decode('utf-8')
        
        prompt = first_photo['caption'] or "Что изображено на фото?"
        
        await processing_msg.edit_text(f"🔄 Модель {settings['model']} анализирует изображение...")
        
        # Отправляем в API
        response_text = await send_image_to_api(settings, image_base64, prompt)
        
        await processing_msg.delete()
        await send_response(chat_id, response_text, settings)
        
    except Exception as e:
        logger.error(f"Ошибка при обработке фото как изображения: {str(e)}", exc_info=True)
        await processing_msg.edit_text(f"❌ Ошибка при обработке изображения: {str(e)}")


async def send_response(chat_id: int, response_text: str, settings: dict, image_count: int = None):
    prefix = ""
    if image_count and image_count > 1:
        #prefix = f"(распознано с {image_count} изображений)\n"
        prefix = ""
    full_text = prefix + response_text
    if settings.get('split_response', False) and len(full_text) > 390:
        chunks = []
        current_chunk = ""
        for char in full_text:
            if len(current_chunk) < 390:
                current_chunk += char
            else:
                chunks.append(current_chunk)
                current_chunk = char
        if current_chunk:
            chunks.append(current_chunk)
        total_parts = len(chunks)
        for i, chunk in enumerate(chunks, 1):
            part_text = f"{i}/{total_parts}\n{chunk}"
            await bot.send_message(chat_id, part_text)
            await asyncio.sleep(4)
    else:
        await bot.send_message(chat_id, full_text)


async def ocr_space_api(image_bytes: bytes, filename: str) -> str:
    url = "https://api.ocr.space/parse/image"
    form_data = aiohttp.FormData()
    form_data.add_field('file', image_bytes, filename=filename, content_type='image/jpeg')
    form_data.add_field('apikey', OCR_API_TOKEN)
    form_data.add_field('language', 'rus')
    form_data.add_field('isOverlayRequired', 'false')
    form_data.add_field('OCREngine', '2')
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, data=form_data) as response:
            if response.status != 200:
                error_text = await response.text()
                raise Exception(f"OCR API error {response.status}: {error_text}")
            result = await response.json()
            if result.get('IsErroredOnProcessing', False):
                error_message = result.get('ErrorMessage', ['Unknown error'])
                if isinstance(error_message, list):
                    error_message = error_message[0]
                raise Exception(f"OCR processing error: {error_message}")
            parsed_results = result.get('ParsedResults', [])
            if not parsed_results:
                raise Exception("No text found in image")
            parsed_text = parsed_results[0].get('ParsedText', '')
            if not parsed_text.strip():
                raise Exception("No text found in image")
            return parsed_text


@router.message(F.text)
async def handle_text_message(message: types.Message):
    if message.text.startswith('/'):
        return
    user_id = message.from_user.id
    settings = user_storage.get_active_profile_settings(user_id)
    if not settings.get('baseurl'):
        await message.answer("❌ Сначала настройте бот с помощью команды /settings")
        return
    processing_msg = await message.answer(f"🔄 Модель {settings['model']} генерирует ответ...")
    try:
        response_text = await send_to_chatgpt(settings, message.text)
        await processing_msg.delete()
        await send_response(message.chat.id, response_text, settings)
    except Exception as e:
        await processing_msg.delete()
        logger.error(f"Ошибка при обращении к ChatGPT: {str(e)}")
        await message.answer(f"❌ Ошибка: {str(e)}")


async def send_to_perplexity(settings: dict, message: str) -> str:
    url = "https://api.perplexity.ai/chat/completions"
    system_prompt = "Be precise and concise. Answer in Russian. Do not use markdown formatting."
    
    # Добавляем промпт модели если есть
    model_prompt = settings.get('model_prompt', '')
    if model_prompt:
        system_prompt = f"{model_prompt}. {system_prompt}"
    
    response_format = settings.get('response_format', 'normal')
    if response_format == 'medium':
        system_prompt += " Provide brief explanations only."
    elif response_format == 'short':
        system_prompt += " Provide direct answers only, no explanations."
    max_tokens = settings.get('max_tokens', 1200)
    data = {
        "model": settings['model'],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": message}
        ],
        "max_tokens": max_tokens,
        "temperature": 0.3,
        "stream": False
    }
    headers = {
        "Authorization": f"Bearer {settings['apikey']}",
        "Content-Type": "application/json"
    }
    timeout = aiohttp.ClientTimeout(total=300)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, headers=headers, json=data) as response:
            if response.status != 200:
                error_text = await response.text()
                raise Exception(f"Perplexity API ошибка {response.status}: {error_text}")
            result = await response.json()
            response_text = result['choices'][0]['message']['content']
            return remove_think_tags(response_text)


async def send_image_to_api(settings: dict, image_base64: str, prompt: str) -> str:
    """Отправка изображения в API в формате base64"""
    if "perplexity.ai" in settings['baseurl']:
        # Perplexity не поддерживает изображения, используем текстовый режим
        return await send_to_perplexity(settings, prompt)
    
    url = f"{settings['baseurl']}/chat/completions"
    
    system_prompt = "Не используй разметку markdown в ответах. Начни отвечать сразу с ответа на вопросы."
    
    # Добавляем промпт модели если есть
    model_prompt = settings.get('model_prompt', '')
    if model_prompt:
        system_prompt = f"{model_prompt}. {system_prompt}"
    
    response_format = settings.get('response_format', 'normal')
    if response_format == 'medium':
        system_prompt += " Не пиши объемный ответ, только вкратце объясни его."
    elif response_format == 'short':
        system_prompt += " Пиши только ответы, без объяснения."
    
    max_tokens = settings.get('max_tokens', 2000)
    
    data = {
        "model": settings['model'],
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user", 
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_base64}",
                            "detail": "auto"
                        }
                    }
                ]
            }
        ],
        "max_tokens": max_tokens,
        "temperature": 0.7
    }
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings['apikey']}"
    }
    
    timeout = aiohttp.ClientTimeout(total=300)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, headers=headers, json=data) as response:
            if response.status != 200:
                error_text = await response.text()
                raise Exception(f"API ошибка {response.status}: {error_text}")
            result = await response.json()
            response_text = result['choices'][0]['message']['content']
            return remove_think_tags(response_text)


async def send_to_chatgpt(settings: dict, message: str) -> str:
    if "perplexity.ai" in settings['baseurl']:
        return await send_to_perplexity(settings, message)
    else:
        url = f"{settings['baseurl']}/chat/completions"
        system_prompt = "Не используй разметку markdown в ответах. Начни отвечать сразу с ответа на вопросы."
        
        # Добавляем промпт модели если есть
        model_prompt = settings.get('model_prompt', '')
        if model_prompt:
            system_prompt = f"{model_prompt}. {system_prompt}"
        
        response_format = settings.get('response_format', 'normal')
        if response_format == 'medium':
            system_prompt += " Не пиши объемный ответ, только вкратце объясни его."
        elif response_format == 'short':
            system_prompt += " Пиши только ответы, без объяснения."
        max_tokens = settings.get('max_tokens', 2000)
        data = {
            "model": settings['model'],
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message}
            ],
            "max_tokens": max_tokens,
            "temperature": 0.7
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings['apikey']}"
        }
        timeout = aiohttp.ClientTimeout(total=300)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=data) as response:
                if response.status != 200:
                    error_text = await response.text()
                    raise Exception(f"API ошибка {response.status}: {error_text}")
                result = await response.json()
                response_text = result['choices'][0]['message']['content']
                return remove_think_tags(response_text)


def remove_think_tags(text: str) -> str:
    import re
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'<think>.*', '', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'\n\s*\n', '\n', text)
    text = text.strip()
    if not text:
        return "Ответ был удален, так как содержал только служебную информацию в тегах <think>"
    return text


# ————————————————————————————————————————————————————————
# Запуск
# ————————————————————————————————————————————————————————

async def main():
    logger.info("Бот запущен")
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
