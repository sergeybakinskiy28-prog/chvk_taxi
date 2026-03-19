from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
import urllib.parse

from chvk_city.backend.config import settings
from chvk_city.bot.telegram.constants import OWNER_ID


def get_user_menu(show_become_driver: bool = True, is_owner: bool = False) -> ReplyKeyboardMarkup:
    """
    Меню обычного пользователя (пассажира).
    show_become_driver: показывать кнопку «Стать водителем» (скрыть после подачи заявки).
    """
    buttons = [
        [KeyboardButton(text="🚖 Заказать такси")],
        [KeyboardButton(text="🗂 Мои заказы")],
        [KeyboardButton(text="📞 Поддержка")],
    ]
    if show_become_driver:
        buttons.append([KeyboardButton(text="🚗 Стать водителем")])
    if is_owner:
        buttons.append([KeyboardButton(text="💎 УПРАВЛЕНИЕ")])
    return ReplyKeyboardMarkup(
        keyboard=buttons,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="УПРАВЛЕНИЕ КНОПКАМИ 👇",
    )


def get_driver_main_menu(is_owner: bool = False) -> ReplyKeyboardMarkup:
    """
    Главное меню водителя: те же кнопки + «Кабинет водителя» вместо «Стать водителем».
    """
    buttons = [
        [KeyboardButton(text="🚖 Заказать такси")],
        [KeyboardButton(text="🗂 Мои заказы")],
        [KeyboardButton(text="📞 Поддержка")],
        [KeyboardButton(text="💼 Кабинет водителя")],
    ]
    if is_owner:
        buttons.append([KeyboardButton(text="💎 УПРАВЛЕНИЕ")])
    return ReplyKeyboardMarkup(
        keyboard=buttons,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="УПРАВЛЕНИЕ КНОПКАМИ 👇",
    )


def get_main_menu(is_driver: bool, has_pending_application: bool, user_id: int | None = None) -> ReplyKeyboardMarkup:
    """
    Универсальная функция для получения главного меню.
    is_driver: True — одобренный водитель (показать «Кабинет водителя»).
    has_pending_application: True — заявка на рассмотрении (скрыть «Стать водителем»).
    """
    is_owner = user_id == OWNER_ID if user_id else False
    if is_driver:
        return get_driver_main_menu(is_owner=is_owner)
    return get_user_menu(show_become_driver=not has_pending_application, is_owner=is_owner)


def get_driver_menu():
    """
    Главное меню водителя: выход/уход со смены.
    """
    buttons = [
        [KeyboardButton(text="▶️ Выйти на смену")],
        [KeyboardButton(text="⏸ Уйти со смены")],
    ]
    return ReplyKeyboardMarkup(
        keyboard=buttons,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Режим работы водителя 👇",
    )


def get_admin_menu():
    """
    Меню администратора.
    """
    buttons = [
        [KeyboardButton(text="👥 Список водителей")],
        [KeyboardButton(text="✅ Одобрить новичков")],
        [KeyboardButton(text="❌ Уволить водителя")],
        [KeyboardButton(text="📊 Статистика заказов")],
    ]
    return ReplyKeyboardMarkup(
        keyboard=buttons,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Админ-панель 👇",
    )


def get_admin_keyboard():
    """
    Клавиатура владельца (управление).
    """
    buttons = [
        [KeyboardButton(text="👥 Водители в штате")],
        [KeyboardButton(text="📩 Новые заявки")],
        [KeyboardButton(text="❌ Удалить водителя (по ID)")],
        [KeyboardButton(text="🔙 Назад")],
    ]
    return ReplyKeyboardMarkup(
        keyboard=buttons,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Управление сервисом 👇",
    )


def get_back_to_menu_keyboard():
    """Клавиатура «Назад в меню» под списком заказов."""
    buttons = [[InlineKeyboardButton(text="◀️ Назад в меню", callback_data="back_to_menu")]]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_start_order_inline_keyboard():
    buttons = [[InlineKeyboardButton(text="🚖 Заказать такси", callback_data="start_order_inline")]]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_accept_order_keyboard(order_id: int):
    buttons = [
        [InlineKeyboardButton(text="✅ Принять заказ", callback_data=f"accept_{order_id}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"ignore_{order_id}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_eta_select_keyboard(order_id: int):
    """Клавиатура выбора интервала времени прибытия к клиенту."""
    buttons = [
        [InlineKeyboardButton(text="🕒 1–3 мин", callback_data=f"eta_1-3_{order_id}")],
        [InlineKeyboardButton(text="🕒 4–6 мин", callback_data=f"eta_4-6_{order_id}")],
        [InlineKeyboardButton(text="🕒 7–10 мин", callback_data=f"eta_7-10_{order_id}")],
        [InlineKeyboardButton(text="🕒 11–15 мин", callback_data=f"eta_11-15_{order_id}")],
        [InlineKeyboardButton(text="🕒 16–20 мин", callback_data=f"eta_16-20_{order_id}")],
        [InlineKeyboardButton(text="🕒 20–30 мин", callback_data=f"eta_20-30_{order_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def _yandex_route(address: str) -> str:
    """
    Формирует ссылку для Яндекс.Карт/Навигатора до указанного адреса.
    Используем формат поиска text=, который заставляет приложение
    открыть поиск по этой строке (и часто сразу предлагает маршрут).
    """
    address_enc = urllib.parse.quote(address)
    # text={address} — поисковый запрос
    # rtt=mt — режим навигации
    return f"https://yandex.ru/maps/?text={address_enc}&rtt=mt"


def get_post_accept_driver_keyboard(order_id: int):
    """
    Базовая клавиатура управления заказом (без навигации).
    Используется как низкий ряд кнопок.
    """
    buttons = [
        [InlineKeyboardButton(text="📍 Я на месте", callback_data=f"at_place_{order_id}")],
        [InlineKeyboardButton(text="✅ Завершить заказ", callback_data=f"complete_{order_id}")],
        [InlineKeyboardButton(text="❌ Отменить поездку", callback_data=f"driver_cancel_{order_id}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_client_after_accept_keyboard(order_id: int, driver_telegram_id: int | None = None):
    """
    Главный блок заказа: только кнопка «Написать водителю» (ссылка на личный чат).
    """
    if driver_telegram_id is None:
        return InlineKeyboardMarkup(inline_keyboard=[])
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Написать водителю", url=f"tg://user?id={driver_telegram_id}")]
    ])


def get_client_in_progress_keyboard(order_id: int, driver_telegram_id: int | None = None):
    """
    Верхний блок в статусе «В пути»: «Написать водителю» заменяется на 💬 Поддержка.
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Поддержка", url=_support_url())],
    ])


def get_at_place_driver_keyboard(order_id: int, client_telegram_id: int | None = None):
    """
    Клавиатура для водителя в статусе 'на месте' (ожидание клиента).
    Только: Начать поездку | Написать клиенту | Отменить. Без звонка и маршрута.
    """
    buttons: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="🚀 Начать поездку", callback_data=f"start_trip_{order_id}")],
        [InlineKeyboardButton(text="❌ Отменить поездку", callback_data=f"driver_cancel_{order_id}")],
    ]
    if client_telegram_id is not None:
        buttons.insert(1, [
            InlineKeyboardButton(text="💬 Написать клиенту", url=f"tg://user?id={client_telegram_id}")
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_in_progress_driver_keyboard(order_id: int, to_address: str):
    """
    Клавиатура для водителя в статусе 'в пути':
    - навигация до конечной точки
    - кнопка завершения заказа
    """
    to_url = _yandex_route(to_address)
    buttons = [
        [InlineKeyboardButton(text="🏁 Маршрут до конца", url=to_url)],
        [InlineKeyboardButton(text="✅ Завершить заказ", callback_data=f"complete_{order_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_driver_districts_keyboard():
    """
    Клавиатура выбора стоянки/района для водителя.
    Кнопки по 2 в ряд.
    """
    rows = [
        [KeyboardButton(text="📍 Губашево"), KeyboardButton(text="📍 Проспект")],
        [KeyboardButton(text="📍 30-й"), KeyboardButton(text="📍 Центр")],
        [KeyboardButton(text="📍 Луч"), KeyboardButton(text="📍 Берсол")],
        [KeyboardButton(text="📍 Владимир"), KeyboardButton(text="📍 Титовка (Начало)")],
        [KeyboardButton(text="📍 Титовка (Конец)"), KeyboardButton(text="📍 Садовка")],
        [KeyboardButton(text="📍 Нагорный"), KeyboardButton(text="📍 Озон")],
    ]
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите стоянку 👇",
    )


def get_driver_accept_keyboard(order_id: int, from_address: str, client_phone: str | None = None) -> InlineKeyboardMarkup:
    """
    Клавиатура для водителя сразу после принятия заказа.
    Порядок сверху вниз: Маршрут → Я на месте → Позвонить клиенту → Отменить.
    Каждая кнопка — отдельная строка для удобства нажатия во время движения.
    """
    from_url = _yandex_route(from_address)
    buttons: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="📍 Маршрут к клиенту", url=from_url)],
        [InlineKeyboardButton(text="✅ Я на месте", callback_data=f"at_place_{order_id}")],
        [InlineKeyboardButton(text="❌ Отменить поездку", callback_data=f"driver_cancel_{order_id}")],
    ]
    if client_phone:
        buttons.insert(2, [InlineKeyboardButton(text="📞 Позвонить клиенту", callback_data=f"driver_call_{order_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_approval_keyboard(driver_id: int):
    buttons = [
        [InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve_{driver_id}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{driver_id}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_confirm_delete_keyboard(telegram_id: int):
    """Клавиатура подтверждения удаления водителя."""
    buttons = [
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"confirm_delete_{telegram_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_delete")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_skip_comment_keyboard():
    buttons = [[InlineKeyboardButton(text="➡️ Пропустить", callback_data="skip_comment")]]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_destination_flow_keyboard():
    buttons = [
        [InlineKeyboardButton(text="➕ Добавить еще остановку", callback_data="add_more_address")],
        [InlineKeyboardButton(text="Далее", callback_data="finish_route")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_from")],
        [InlineKeyboardButton(text="❌ Отменить заказ", callback_data="cancel_order_creation")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# Цены опций (должны совпадать с _estimate_order_price в handlers.py)
CHILD_SEAT_PRICE = 50
PET_PRICE = 30


def get_order_options_keyboard(has_child_seat: bool = False, has_pet: bool = False):
    child_text = f"✅ 👶 Детское кресло (+{CHILD_SEAT_PRICE} ₽)" if has_child_seat else "👶 Детское кресло"
    pet_text = f"✅ 🐾 С питомцем (+{PET_PRICE} ₽)" if has_pet else "🐾 С питомцем"
    buttons = [
        [InlineKeyboardButton(text=child_text, callback_data="toggle_child_seat")],
        [InlineKeyboardButton(text=pet_text, callback_data="toggle_pet")],
        [InlineKeyboardButton(text="✍️ Добавить комментарий", callback_data="add_order_comment")],
        [InlineKeyboardButton(text="Далее", callback_data="calculate_order_price")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_to_address")],
        [InlineKeyboardButton(text="❌ Отменить заказ", callback_data="cancel_order_creation")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_order_confirmation_keyboard():
    buttons = [
        [InlineKeyboardButton(text="🚖 Подтвердить заказ", callback_data="confirm_order_creation")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_options")],
        [InlineKeyboardButton(text="❌ Отменить заказ", callback_data="cancel_order_creation")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_order_manage_keyboard(order_id: int):
    buttons = [[InlineKeyboardButton(text="❌ Отменить заказ", callback_data=f"cancel_{order_id}")]]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _support_url() -> str:
    """URL кнопки «Поддержка»: t.me/ADMIN_USERNAME или t.me/telegram по умолчанию."""
    admin_username = getattr(settings, "ADMIN_USERNAME", "") or ""
    username = admin_username.strip().lstrip("@") if admin_username.strip() else "telegram"
    return f"https://t.me/{username}"


def get_rate_trip_keyboard(order_id: int):
    """
    Клавиатура для оценки поездки клиентом (только звёзды).
    После оценки — финальная плашка с одной кнопкой 💬 Поддержка.
    """
    buttons = [
        [
            InlineKeyboardButton(text="⭐ 1", callback_data=f"rate_1_{order_id}"),
            InlineKeyboardButton(text="⭐ 2", callback_data=f"rate_2_{order_id}"),
            InlineKeyboardButton(text="⭐ 3", callback_data=f"rate_3_{order_id}"),
            InlineKeyboardButton(text="⭐ 4", callback_data=f"rate_4_{order_id}"),
            InlineKeyboardButton(text="⭐ 5", callback_data=f"rate_5_{order_id}"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_support_only_keyboard():
    """Клавиатура только с кнопкой «Поддержка» (для сообщений после оценки)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Поддержка", url=_support_url())],
    ])


def get_new_order_after_rating_keyboard():
    """
    Клавиатура с одной кнопкой для запуска нового заказа после оценки.
    """
    buttons = [
        [InlineKeyboardButton(text="🚖 Заказать такси", callback_data="start_new_order")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_client_at_place_keyboard(order_id: int, driver_telegram_id: int | None = None):
    """
    Клавиатура для клиента, когда водитель на месте.
    Только 🏃 Выхожу! — связь через верхнее сообщение (Написать водителю).
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏃 Выхожу!", callback_data=f"client_out_{order_id}")],
    ])


def get_client_after_out_keyboard(order_id: int):
    """
    Клавиатура для клиента после нажатия '🏃 Выхожу!':
    связь с водителем и поддержка.
    """
    buttons: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="📱 Позвонить", callback_data=f"client_call_{order_id}")],
        [InlineKeyboardButton(text="💬 Поддержка", url=_support_url())],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)
def get_phone_keyboard():
    buttons = [[KeyboardButton(text="📱 Отправить номер телефона", request_contact=True)]]
    return ReplyKeyboardMarkup(
        keyboard=buttons,
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Используйте кнопки меню 👇",
        is_persistent=True,
    )
