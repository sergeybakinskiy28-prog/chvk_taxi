from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
import urllib.parse

def get_main_menu():
    buttons = [
        [KeyboardButton(text="🚕 Заказать такси")],
        [KeyboardButton(text="🗂 Мои заказы")],
        [KeyboardButton(text="ℹ️ Помощь")]
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def get_back_to_menu_keyboard():
    """Клавиатура «Назад в меню» под списком заказов."""
    buttons = [[InlineKeyboardButton(text="◀️ Назад в меню", callback_data="back_to_menu")]]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_accept_order_keyboard(order_id: int):
    buttons = [
        [InlineKeyboardButton(text="✅ Принять заказ", callback_data=f"accept_{order_id}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"ignore_{order_id}")]
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


def get_client_after_accept_keyboard(order_id: int):
    """
    Клавиатура для клиента сразу после принятия заказа водителем:
    только кнопка связи.
    """
    buttons: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="📞 Связаться", callback_data=f"client_call_{order_id}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_at_place_driver_keyboard(order_id: int):
    """
    Клавиатура для водителя в статусе 'на месте':
    только управление — без навигации.
    """
    buttons = [
        [InlineKeyboardButton(text="▶️ Начать поездку", callback_data=f"start_trip_{order_id}")],
        [InlineKeyboardButton(text="❌ Отменить поездку", callback_data=f"driver_cancel_{order_id}")],
    ]
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


def get_driver_accept_keyboard(order_id: int, from_address: str) -> InlineKeyboardMarkup:
    """
    Клавиатура для водителя сразу после принятия заказа:
    - навигация к клиенту
    - кнопка 'Я на месте'
    - (опционально) отмена
    """
    from_url = _yandex_route(from_address)
    buttons = [
        [InlineKeyboardButton(text="📍 Маршрут к клиенту", url=from_url)],
        [InlineKeyboardButton(text="📍 Я на месте", callback_data=f"at_place_{order_id}")],
        [InlineKeyboardButton(text="❌ Отменить поездку", callback_data=f"driver_cancel_{order_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_approval_keyboard(driver_id: int):
    buttons = [
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"approve_{driver_id}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{driver_id}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_skip_comment_keyboard():
    buttons = [[InlineKeyboardButton(text="➡️ Пропустить", callback_data="skip_comment")]]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_order_manage_keyboard(order_id: int):
    buttons = [[InlineKeyboardButton(text="❌ Отменить заказ", callback_data=f"cancel_{order_id}")]]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_rate_trip_keyboard(order_id: int):
    """
    Клавиатура для оценки поездки клиентом (1–5 звёзд).
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


def get_new_order_after_rating_keyboard():
    """
    Клавиатура с одной кнопкой для запуска нового заказа после оценки.
    """
    buttons = [
        [InlineKeyboardButton(text="🚕 Заказать новое такси", callback_data="start_new_order")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_client_at_place_keyboard(order_id: int, driver_phone: str | None):
    """
    Клавиатура для клиента, когда водитель на месте.
    - '🏃 Выхожу!' — уведомление водителю
    - '📱 Позвонить' — присылаем номер водителя текстом отдельным сообщением
    """
    buttons: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="🏃 Выхожу!", callback_data=f"client_out_{order_id}")],
        [InlineKeyboardButton(text="📱 Позвонить", callback_data=f"client_call_{order_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_client_after_out_keyboard(order_id: int):
    """
    Клавиатура для клиента после нажатия '🏃 Выхожу!':
    оставляем только кнопку связи.
    """
    buttons: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="📱 Позвонить", callback_data=f"client_call_{order_id}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)
def get_phone_keyboard():
    buttons = [[KeyboardButton(text="📱 Отправить номер телефона", request_contact=True)]]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True, one_time_keyboard=True)
