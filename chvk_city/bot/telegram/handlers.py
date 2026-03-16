import asyncio
import logging
import httpx
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, ReplyKeyboardRemove, ReplyKeyboardMarkup
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import StorageKey
from aiogram.utils.keyboard import InlineKeyboardBuilder
from chvk_city.backend.config import settings
from chvk_city.backend.database.session import async_session
from chvk_city.backend.services.taxi_service import TaxiService
from chvk_city.bot.telegram import keyboards
from chvk_city.bot.telegram.constants import OWNER_ID
from chvk_city.bot.telegram.zones_data import (
    get_zone_by_address,
    get_zone_price,
    get_ride_minutes,
    DEFAULT_ZONE_PRICE,
    DEFAULT_PRICE_NOTE,
)

logger = logging.getLogger(__name__)


def _is_owner(user_id: int) -> bool:
    """Проверка доступа владельца (OWNER_ID)."""
    return user_id == OWNER_ID
router = Router()

online_drivers: set[int] = set()

class OrderTaxi(StatesGroup):
    waiting_for_from_address = State()
    waiting_for_to_address = State()
    waiting_for_options = State()
    waiting_for_comment = State()
    waiting_for_confirmation = State()

class DriverRegistration(StatesGroup):
    """Пошаговый опрос для регистрации водителя."""
    waiting_for_full_name = State()
    waiting_for_car_info = State()
    waiting_for_phone = State()


class DriverShift(StatesGroup):
    waiting_for_district = State()


class OwnerDeleteDriver(StatesGroup):
    waiting_for_driver_id = State()

@router.message(OrderTaxi.waiting_for_from_address, F.text)
async def process_from_address(message: Message, state: FSMContext):
    # Игнорируем нажатия кнопок меню — ждём текстовый адрес
    if message.text in {
        "🚖 Заказать такси",
        "🚕 Заказать такси",
        "🗂 Мои заказы",
        "📞 Поддержка",
        "💼 Кабинет водителя",
        "🚗 Стать водителем",
        "⚙️ Админка",
        "💎 УПРАВЛЕНИЕ",
    }:
        await message.answer("Сейчас я жду адрес, пожалуйста, напишите его текстом.")
        return
    from_address = (message.text or "").strip()
    if len(from_address) < 3:
        await message.answer("Пожалуйста, напишите адрес отправления текстом.")
        return
    data = await state.get_data()
    prev_msg_ids = data.get("msg_to_delete", [])
    await state.update_data(
        from_address=from_address,
    )
    try:
        await message.delete()
    except Exception:
        pass
    await _delete_messages(message.bot, message.chat.id, prev_msg_ids)
    await state.update_data(msg_to_delete=[])
    print(f"DEBUG ORDER: saved from_address='{from_address}' for user {message.from_user.id}", flush=True)
    await _prompt_for_to_address(message, state, message.from_user.id)


@router.message(OrderTaxi.waiting_for_to_address, F.text)
async def process_to_address(message: Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("from_address"):
        await state.clear()
        await message.answer(
            "Сессия сброшена. Нажмите «🚖 Заказать такси» для нового заказа.",
            reply_markup=await _get_menu_for_user(message.from_user.id),
        )
        return

    # Игнорируем нажатия кнопок меню — ждём текстовый адрес
    if message.text in {
        "🚖 Заказать такси",
        "🚕 Заказать такси",
        "🗂 Мои заказы",
        "📞 Поддержка",
        "💼 Кабинет водителя",
        "🚗 Стать водителем",
        "⚙️ Админка",
        "💎 УПРАВЛЕНИЕ",
    }:
        await message.answer("Сейчас я жду второй адрес, пожалуйста, напишите его текстом.")
        return
    to_address = (message.text or "").strip()
    if len(to_address) < 3:
        await message.answer("Пожалуйста, напишите адрес назначения текстом.")
        return
    data = await state.get_data()
    prev_msg_ids = data.get("msg_to_delete", [])
    edit_id = data.get("route_message_id") or (prev_msg_ids[-1] if prev_msg_ids else None)
    try:
        await message.delete()
    except Exception:
        pass
    await _save_destination_and_show_options(
        message, state, to_address, message.from_user.id, edit_message_id=edit_id
    )


@router.message(OrderTaxi.waiting_for_comment, F.text)
async def process_comment(message: Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("from_address") or not data.get("destination_addresses"):
        await state.clear()
        await message.answer(
            "Сессия сброшена. Нажмите «🚖 Заказать такси» для нового заказа.",
            reply_markup=await _get_menu_for_user(message.from_user.id),
        )
        return

    # Игнорируем нажатия кнопок меню — ждём комментарий или текст
    if message.text in {
        "🚖 Заказать такси",
        "🚕 Заказать такси",
        "🗂 Мои заказы",
        "📞 Поддержка",
        "💼 Кабинет водителя",
        "🚗 Стать водителем",
        "⚙️ Админка",
        "💎 УПРАВЛЕНИЕ",
    }:
        await message.answer("Сейчас я жду текст комментария к заказу.")
        return
    data = await state.get_data()
    msg_list = data.get("msg_to_delete", [])
    msg_list.append(message.message_id)
    await state.update_data(
        msg_to_delete=msg_list,
        order_comment=(message.text or "").strip(),
    )
    await _show_order_options_screen(message, state)


_http_client: httpx.AsyncClient | None = None

def init_http_client() -> None:
    """Создать единую сессию httpx при старте бота (избегаем задержек на каждый запрос)."""
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            base_url=settings.API_BASE_URL,
            trust_env=False,
            timeout=httpx.Timeout(30.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

def get_http_client() -> httpx.AsyncClient:
    if _http_client is None:
        init_http_client()
    return _http_client


async def _get_menu_for_user(telegram_id: int) -> ReplyKeyboardMarkup:
    """
    Возвращает главное меню в зависимости от статуса пользователя.
    Проверка идёт напрямую по БД через TaxiService.get_driver.
    - is_driver (одобрен) → меню с «Кабинет водителя»
    - has_pending_application → меню без «Стать водителем»
    - иначе → меню с «Стать водителем»
    """
    is_driver = False
    has_pending = False
    source = "DB(no driver)"

    try:
        async with async_session() as db:
            driver = await TaxiService.get_driver(db, telegram_id)
            if driver:
                if driver.is_approved:
                    is_driver = True
                    source = "DB(is_driver=True)"
                else:
                    has_pending = True
                    source = "DB(has_pending=True)"
    except Exception as e:
        logger.error(f"DB driver check failed for {telegram_id}: {e}", exc_info=True)
        source = "fallback"

    print(
        f"DEBUG: Checking menu for {telegram_id}, is_driver={is_driver}, source={source}",
        flush=True,
    )
    print(f"Sending menu to {telegram_id}", flush=True)
    return keyboards.get_main_menu(
        is_driver=is_driver,
        has_pending_application=has_pending,
        user_id=telegram_id,
    )


async def _get_driver_flags(telegram_id: int) -> tuple[bool, bool]:
    """
    Возвращает флаги статуса водителя:
    - is_driver: водитель одобрен
    - has_pending: заявка создана, но ещё не одобрена
    """
    try:
        async with async_session() as db:
            driver = await TaxiService.get_driver(db, telegram_id)
            if not driver:
                return False, False
            if driver.is_approved:
                return True, False
            return False, True
    except Exception as e:
        logger.error(f"Failed to get driver flags for {telegram_id}: {e}", exc_info=True)
        return False, False


def _normalize_phone_digits(phone: str) -> str:
    """Извлекает только цифры из номера, игнорируя +, (, ), -, пробелы."""
    return "".join(c for c in str(phone) if c.isdigit())


def _is_valid_phone(phone: str) -> bool:
    """Проверка: в номере от 10 до 12 цифр."""
    digits = _normalize_phone_digits(phone)
    return 10 <= len(digits) <= 12


def _eta_key_to_display(eta_key: str) -> str:
    """Преобразует ключ интервала (1-3, 4-6) в текст для пассажира (1–3 мин)."""
    mapping = {
        "1-3": "1–3 мин",
        "4-6": "4–6 мин",
        "7-10": "7–10 мин",
        "11-15": "11–15 мин",
        "16-20": "16–20 мин",
        "20-30": "20–30 мин",
    }
    return mapping.get(eta_key, eta_key + " мин")


def _get_passenger_state(bot, storage, user_id):
    key = StorageKey(
        bot_id=bot.id,
        chat_id=user_id,
        user_id=user_id,
    )
    return FSMContext(storage=storage, key=key)


async def _delete_messages(bot, chat_id: int, message_ids: list[int] | None):
    if not message_ids:
        return
    for mid in message_ids:
        if not isinstance(mid, int):
            continue
        try:
            await bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            pass


async def _delete_or_clear_buttons(bot, chat_id: int, message_id: int):
    """
    Удаляет сообщение. Если не удалось (например, старше 48 ч) — очищает кнопки.
    """
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=None,
            )
        except Exception:
            pass


async def _remove_inline_keyboard(bot, chat_id: int, message_id: int) -> bool:
    """Удаляет Inline-клавиатуру у сообщения. Возвращает True при успехе."""
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=None,
        )
        return True
    except Exception:
        return False


async def _get_recent_addresses(telegram_id: int, address_type: str) -> list[str]:
    """
    Возвращает до 3 уникальных недавних адресов пользователя:
    - address_type == "from" -> адреса отправления
    - address_type == "to" -> адреса назначения
    """
    try:
        async with async_session() as db:
            return await TaxiService.get_recent_unique_addresses(
                db,
                telegram_id,
                address_type=address_type,
                limit=3,
            )
    except Exception as e:
        logger.error(f"Failed to load recent addresses for {telegram_id}: {e}", exc_info=True)
        return []


def _short_address_label(address: str) -> str:
    address = address.strip()
    if len(address) <= 32:
        return address
    return f"{address[:29]}..."


def _build_recent_addresses_keyboard(addresses: list[str], step: str):
    builder = InlineKeyboardBuilder()
    prefix = "recent_from" if step == "from" else "recent_to"
    manual_callback = "manual_from" if step == "from" else "manual_to"
    icon = "🏠" if step == "from" else "📍"

    for idx, address in enumerate(addresses):
        builder.button(
            text=f"{icon} {_short_address_label(address)}",
            callback_data=f"{prefix}_{idx}",
        )

    builder.button(
        text="⌨️ Ввести адрес вручную",
        callback_data=manual_callback,
    )
    builder.adjust(1)
    return builder.as_markup()


async def _prompt_for_from_address(target_message: Message, state: FSMContext, telegram_id: int):
    recent_addresses = await _get_recent_addresses(telegram_id, "from")
    data = await state.get_data()
    msg_list = data.get("msg_to_delete", [])

    sent = await target_message.answer(
        "📍 Откуда вас забрать?\nВыберите из списка ниже или введите новый:",
        reply_markup=_build_recent_addresses_keyboard(recent_addresses, "from"),
    )
    msg_list.append(sent.message_id)
    await state.update_data(
        msg_to_delete=msg_list,
        recent_from_addresses=recent_addresses,
    )

    await state.set_state(OrderTaxi.waiting_for_from_address)


async def _prompt_for_to_address(target_message: Message, state: FSMContext, telegram_id: int):
    recent_addresses = await _get_recent_addresses(telegram_id, "to")
    data = await state.get_data()
    msg_list = data.get("msg_to_delete", [])

    sent = await target_message.answer(
        "🏁 Куда едем?\nВыберите из списка ниже или введите новый:",
        reply_markup=_build_recent_addresses_keyboard(recent_addresses, "to"),
    )
    msg_list.append(sent.message_id)
    await state.update_data(
        msg_to_delete=msg_list,
        recent_to_addresses=recent_addresses,
    )

    await state.set_state(OrderTaxi.waiting_for_to_address)


def _build_route_with_add_more_prompt(from_address: str, destination_addresses: list[str]) -> str:
    """Текст для этапа «добавить ещё остановку»: маршрут + подсказка."""
    route = _format_route_vertical(from_address, destination_addresses)
    return f"{route}\n\nВыберите адрес из списка ниже или введите новый вручную:"


async def _save_destination_and_show_options(
    target_message: Message,
    state: FSMContext,
    address: str,
    user_id: int,
    edit_message_id: int | None = None,
):
    data = await state.get_data()
    msg_list = data.get("msg_to_delete", [])
    destination_addresses = data.get("destination_addresses", [])
    destination_addresses.append(address)
    await state.update_data(
        destination_addresses=destination_addresses,
        to_address="\n".join(destination_addresses),
    )
    print(
        f"DEBUG ORDER: destination added='{address}', route={destination_addresses}, user={user_id}",
        flush=True,
    )
    from_address = data.get("from_address", "—")
    route_text = (
        "📍 Адрес добавлен в маршрут.\n\n"
        "Ваш маршрут:\n"
        f"{_format_route_vertical(from_address, destination_addresses)}"
    )
    keyboard = keyboards.get_destination_flow_keyboard()

    if edit_message_id:
        try:
            await target_message.bot.edit_message_text(
                chat_id=target_message.chat.id,
                message_id=edit_message_id,
                text=route_text,
                reply_markup=keyboard,
            )
        except Exception:
            await _delete_messages(target_message.bot, target_message.chat.id, [edit_message_id])
            sent = await target_message.answer(route_text, reply_markup=keyboard)
            edit_message_id = sent.message_id
        await state.update_data(msg_to_delete=[edit_message_id], route_message_id=edit_message_id)
    else:
        await _delete_messages(target_message.bot, target_message.chat.id, msg_list)
        sent = await target_message.answer(route_text, reply_markup=keyboard)
        await state.update_data(msg_to_delete=[sent.message_id], route_message_id=sent.message_id)


def _build_order_options_text(from_address: str, destination_addresses: list[str], order_comment: str | None = None) -> str:
    text = (
        "🛠 Опции заказа\n"
        "Вы можете выбрать дополнительные параметры или добавить комментарий для водителя.\n\n"
        + _format_route_vertical(from_address, destination_addresses)
    )
    if order_comment:
        text += f"\n\n💬 Комментарий: {order_comment}"
    return text


def _format_route_vertical(from_address: str, destination_addresses: list[str]) -> str:
    lines = [f"📍 Откуда: {from_address}"]
    if not destination_addresses:
        return "\n".join(lines)

    if len(destination_addresses) == 1:
        lines.append(f"🏁 Финиш: {destination_addresses[0]}")
        return "\n".join(lines)

    for idx, address in enumerate(destination_addresses[:-1], start=1):
        lines.append(f"🛑 Точка {idx}: {address}")
    lines.append(f"🏁 Финиш: {destination_addresses[-1]}")
    return "\n".join(lines)


def _split_destination_addresses(to_address: str | None) -> list[str]:
    if not to_address:
        return []
    normalized = to_address.replace(" -> ", "\n")
    return [part.strip() for part in normalized.splitlines() if part.strip()]


def _format_route_from_values(from_address: str | None, to_address: str | None) -> str:
    return _format_route_vertical(from_address or "—", _split_destination_addresses(to_address))


def _estimate_order_price(data: dict) -> tuple[float, str | None]:
    """
    Рассчитывает стоимость заказа.
    Возвращает (цена, примечание или None).
    Если оба адреса опознаны по зонам — цена из ZONE_PRICES.
    Иначе — стандартная цена и примечание «Цена рассчитана по городскому тарифу».
    """
    from_address = data.get("from_address") or ""
    destination_addresses = data.get("destination_addresses") or []
    first_to = destination_addresses[0] if destination_addresses else ""

    from_zone = get_zone_by_address(from_address)
    to_zone = get_zone_by_address(first_to)
    zone_price, recognized = get_zone_price(from_zone, to_zone)

    if recognized:
        base_price = zone_price
        price_note = None
    else:
        base_price = DEFAULT_ZONE_PRICE
        price_note = DEFAULT_PRICE_NOTE

    extra_stop_price = 40.0 * max(0, len(destination_addresses) - 1)
    child_seat_price = 50.0 if data.get("has_child_seat") else 0.0
    pet_price = 30.0 if data.get("has_pet") else 0.0
    total = base_price + extra_stop_price + child_seat_price + pet_price
    return total, price_note


def _build_final_summary_text(data: dict) -> str:
    from_address = data.get("from_address") or "—"
    destination_addresses = data.get("destination_addresses") or []
    order_comment = data.get("order_comment")
    price = data.get("calculated_price")
    price_note = data.get("price_note")
    options: list[str] = []
    if data.get("has_child_seat"):
        options.append("👶 Детское кресло")
    if data.get("has_pet"):
        options.append("🐾 С питомцем")
    if order_comment:
        options.append(f"💬 {order_comment}")
    options_text = "\n".join(options) if options else "—"
    price_text = f"{price:.0f} руб." if isinstance(price, (int, float)) else "—"
    if price_note:
        price_text += f"\n_{price_note}_"

    return (
        "✅ Итог заказа\n\n"
        f"{_format_route_vertical(from_address, destination_addresses)}\n\n"
        f"🛠 Опции:\n{options_text}\n\n"
        f"💰 Стоимость: {price_text}"
    )


def _build_order_comment_payload(data: dict, explicit_comment: str | None = None) -> str | None:
    order_comment = explicit_comment if explicit_comment is not None else data.get("order_comment")
    parts: list[str] = []
    if data.get("has_child_seat"):
        parts.append("👶 Детское кресло")
    if data.get("has_pet"):
        parts.append("🐾 С питомцем")
    if order_comment:
        parts.append(f"💬 {order_comment}")
    if not parts:
        return None
    return "\n".join(parts)


async def _show_order_options_screen(target_message: Message, state: FSMContext):
    data = await state.get_data()
    from_address = data.get("from_address") or "—"
    destination_addresses = data.get("destination_addresses") or []
    has_child_seat = bool(data.get("has_child_seat"))
    has_pet = bool(data.get("has_pet"))
    order_comment = data.get("order_comment")

    await _delete_messages(target_message.bot, target_message.chat.id, data.get("msg_to_delete", []))
    sent = await target_message.answer(
        _build_order_options_text(from_address, destination_addresses, order_comment),
        reply_markup=keyboards.get_order_options_keyboard(
            has_child_seat=has_child_seat,
            has_pet=has_pet,
        ),
    )
    msg_list: list[int] = []
    msg_list.append(sent.message_id)
    await state.update_data(msg_to_delete=msg_list)
    await state.set_state(OrderTaxi.waiting_for_options)


async def _begin_order_flow(
    target_message: Message,
    state: FSMContext,
    user_id: int,
    trigger_message_id: int | None = None,
):
    msg_list: list[int] = []
    if isinstance(trigger_message_id, int):
        msg_list.append(trigger_message_id)

    await state.update_data(
        msg_to_delete=msg_list,
        order_started_by_button=True,
        requester_telegram_id=user_id,
        destination_addresses=[],
        from_address=None,
        to_address=None,
        has_child_seat=False,
        has_pet=False,
        order_comment=None,
        calculated_price=None,
        price_note=None,
        is_processing=False,
        order_id=None,
    )
    await _prompt_for_from_address(target_message, state, user_id)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    """
    Универсальная точка входа:
    - сбрасывает текущее состояние;
    - регистрирует пользователя при необходимости;
    - всегда показывает главное меню с кнопкой заказа.
    """
    await state.clear()

    user_id = message.from_user.id

    try:
        await get_http_client().post(
            "/taxi/user/register",
            json={
                "telegram_id": user_id,
                "name": message.from_user.full_name or None,
            }
        )
    except Exception as e:
        logger.error(f"Failed to register user {user_id}: {e}")
        print(f"[API] register user: {e}", flush=True)
        # Не прерываем /start: главное меню всё равно должно показываться

    welcome = await message.answer(
        "Привет! Я помогу вам быстро заказать такси. Нажмите на кнопку ниже, чтобы начать.",
        reply_markup=keyboards.get_start_order_inline_keyboard(),
    )
    menu_msg = await message.answer(
        "\u200b",
        reply_markup=await _get_menu_for_user(message.from_user.id),
    )
    await state.update_data(
        last_menu_msg_id=welcome.message_id,
        start_message_ids=[welcome.message_id, menu_msg.message_id],
    )

@router.message(F.contact)
async def process_contact(message: Message, state: FSMContext):
    phone = _normalize_phone_digits(message.contact.phone_number or "")
    current_state = await state.get_state()
    if current_state == DriverRegistration.waiting_for_phone.state:
        await _finalize_driver_registration(message, state, phone)
        return
    try:
        await get_http_client().post(
            "/taxi/user/update_phone",
            json={
                "telegram_id": message.from_user.id,
                "phone": phone
            }
        )
    except Exception as e:
        logger.error(f"Failed to update phone for user {message.from_user.id}: {e}")
        await message.answer("An error occurred. Please try again later.")
        return
    
    await message.answer(
        f"✅ Номер {phone} подтвержден! Теперь вы можете заказать такси.",
        reply_markup=await _get_menu_for_user(message.from_user.id)
    )

def _get_technical_messages_kill_list(data: dict) -> list[int]:
    """
    Kill list: только технический мусор для удаления.
    НЕ включает карточки поездок («Итог заказа», «Заказ создан», «Водитель принял», «Поездка завершена»).
    """
    ids: list[int] = []
    for key in ("last_menu_msg_id", "last_new_order_prompt_id"):
        mid = data.get(key)
        if isinstance(mid, int) and mid not in ids:
            ids.append(mid)
    for mid in data.get("start_message_ids") or []:
        if isinstance(mid, int) and mid not in ids:
            ids.append(mid)
    return ids


async def _delete_or_clear_buttons_safe(bot, chat_id: int, message_id: int):
    """Удаляет сообщение. При ошибке API — очищает кнопки. try-except на каждый вызов."""
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=None,
            )
        except Exception:
            pass


@router.message(F.text == "🚖 Заказать такси")
@router.message(F.text == "🚕 Заказать такси")
async def taxi_order_start(message: Message, state: FSMContext):
    # Kill list: приветствие, «Заказать такси» после поездки, «Дополнительные функции»
    data = await state.get_data()
    for mid in _get_technical_messages_kill_list(data):
        await _delete_or_clear_buttons_safe(message.bot, message.chat.id, mid)
    await state.clear()

    await _begin_order_flow(
        message,
        state,
        message.from_user.id,
        trigger_message_id=message.message_id,
    )


@router.callback_query(F.data == "start_order_inline")
async def start_order_inline_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    chat_id = callback.message.chat.id
    data = await state.get_data()
    for mid in _get_technical_messages_kill_list(data):
        await _delete_or_clear_buttons_safe(callback.bot, chat_id, mid)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await state.clear()
    await _begin_order_flow(
        callback.message,
        state,
        callback.from_user.id,
    )


@router.message(F.text == "🗂 Мои заказы")
async def my_orders_handler(message: Message):
    """Показ истории завершённых заказов пользователя."""
    telegram_id = message.from_user.id
    try:
        user_resp = await get_http_client().get(f"/taxi/user/{telegram_id}")
        if user_resp.status_code != 200:
            await message.answer(
                "Вы еще не совершали поездок. Самое время заказать такси! 🚕",
                reply_markup=await _get_menu_for_user(message.from_user.id),
            )
            return
        user_data = user_resp.json()
        user_id = user_data.get("id")
        if not user_id:
            await message.answer(
                "Вы еще не совершали поездок. Самое время заказать такси! 🚕",
                reply_markup=await _get_menu_for_user(message.from_user.id),
            )
            return

        history_resp = await get_http_client().get(f"/taxi/orders/history/{user_id}")
        if history_resp.status_code != 200:
            await message.answer(
                "Не удалось загрузить историю заказов. Попробуйте позже.",
                reply_markup=await _get_menu_for_user(message.from_user.id),
            )
            return

        orders = history_resp.json()
        if not orders:
            await message.answer(
                "Вы еще не совершали поездок. Самое время заказать такси! 🚕",
                reply_markup=await _get_menu_for_user(message.from_user.id),
            )
            return

        lines = ["📋 Ваша история поездок\n"]
        for o in orders:
            raw = o.get("created_at", "")  # YYYY-MM-DDTHH:MM:SS
            created = raw[:10]  # YYYY-MM-DD
            if len(raw) >= 16:
                # формат 12.03.2025 14:30
                created = f"{raw[8:10]}.{raw[5:7]}.{raw[:4]} {raw[11:16]}"
            price = o.get("price")
            price_str = f"{price:.0f} руб." if price is not None else "—"
            lines.append(f"📅 Поездка от {created}")
            lines.append(f"📍 Откуда: {o.get('from_address', '—')}")
            lines.append(f"🏁 Куда: {o.get('to_address', '—')}")
            lines.append(f"💰 Стоимость: {price_str}")
            lines.append("---")
        text = "\n".join(lines).strip()
        if text.endswith("---"):
            text = text[:-3].strip()
        await message.answer(
            text,
            reply_markup=keyboards.get_back_to_menu_keyboard(),
        )
    except Exception as e:
        logger.exception(f"Error loading order history for {telegram_id}: {e}")
        await message.answer(
            "Не удалось загрузить историю заказов. Попробуйте позже.",
            reply_markup=await _get_menu_for_user(message.from_user.id),
        )


@router.message(F.text == "📞 Поддержка")
async def support_handler(message: Message):
    """Кнопка «Поддержка» — краткая информация и меню."""
    await message.answer(
        "📞 Поддержка CHVK City\n\nПо вопросам заказа и работы сервиса обращайтесь к администратору.",
        reply_markup=await _get_menu_for_user(message.from_user.id),
    )


@router.message(Command("driver"))
async def cmd_driver_handler(message: Message, state: FSMContext):
    """
    Команда /driver (кнопка «Стать водителем» в меню):
    - если водитель одобрен → кабинет водителя;
    - если заявка на рассмотрении → сообщение ожидания;
    - иначе → запуск опроса регистрации.
    """
    telegram_id = message.from_user.id
    try:
        async with async_session() as db:
            driver = await TaxiService.get_driver(db, telegram_id)
    except Exception as e:
        logger.error(f"Failed to load driver {telegram_id}: {e}", exc_info=True)
        await message.answer(
            "❌ Временно недоступно. Попробуйте позже.",
            reply_markup=await _get_menu_for_user(telegram_id),
        )
        return

    if driver:
        if driver.is_approved:
            await message.answer(
                "💼 Кабинет водителя\n\n"
                "Здесь вы можете выйти на смену или приостановить приём заказов.",
                reply_markup=keyboards.get_driver_menu(),
            )
            return
        await message.answer(
            "🕓 Ваша заявка на рассмотрении.\n"
            "Пожалуйста, дождитесь одобрения.",
            reply_markup=await _get_menu_for_user(telegram_id),
        )
        return

    await _start_driver_registration(message, state)


@router.message(F.text == "🚗 Стать водителем")
async def become_driver_handler(message: Message, state: FSMContext):
    """Кнопка «Стать водителем» — запуск пошагового опроса регистрации."""
    telegram_id = message.from_user.id
    try:
        async with async_session() as db:
            driver = await TaxiService.get_driver(db, telegram_id)
        if driver:
            if driver.is_approved:
                await message.answer(
                    "Вы уже одобренный водитель. Откройте кабинет.",
                    reply_markup=await _get_menu_for_user(telegram_id),
                )
                return
            await message.answer(
                "🕓 Ваша заявка уже на рассмотрении. Дождитесь одобрения.",
                reply_markup=await _get_menu_for_user(telegram_id),
                )
            return
    except Exception as e:
        logger.error(f"become_driver_handler DB check failed for {telegram_id}: {e}", exc_info=True)
        await message.answer("An error occurred. Please try again later.")
        return
    await _start_driver_registration(message, state)


async def _start_driver_registration(message: Message, state: FSMContext):
    """Начать FSM-опрос регистрации водителя."""
    await state.clear()
    await state.set_state(DriverRegistration.waiting_for_full_name)
    await message.answer(
        "🚗 Регистрация водителя\n\n"
        "Шаг 1 из 3. Укажите ваше ФИО полностью:",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(DriverRegistration.waiting_for_full_name, F.text)
async def driver_reg_full_name(message: Message, state: FSMContext):
    if message.text in {"🚕 Заказать такси", "🚖 Заказать такси", "🗂 Мои заказы", "📞 Поддержка", "🚗 Стать водителем", "💎 УПРАВЛЕНИЕ"}:
        await state.clear()
        await message.answer("Регистрация отменена.", reply_markup=await _get_menu_for_user(message.from_user.id))
        return
    if not message.text or len(message.text.strip()) < 3:
        await message.answer("Пожалуйста, введите ФИО (полностью, минимум 3 символа).")
        return
    await state.update_data(full_name=message.text.strip())
    await state.set_state(DriverRegistration.waiting_for_car_info)
    await message.answer(
        "Шаг 2 из 3. Укажите марку автомобиля и госномер.\n"
        "Например: Toyota Camry А123BC777",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(DriverRegistration.waiting_for_car_info, F.text)
async def driver_reg_car_info(message: Message, state: FSMContext):
    if message.text in {"🚕 Заказать такси", "🚖 Заказать такси", "🗂 Мои заказы", "📞 Поддержка", "🚗 Стать водителем", "💎 УПРАВЛЕНИЕ"}:
        await state.clear()
        await message.answer("Регистрация отменена.", reply_markup=await _get_menu_for_user(message.from_user.id))
        return
    if not message.text or len(message.text.strip()) < 5:
        await message.answer("Пожалуйста, укажите марку и госномер автомобиля.")
        return
    await state.update_data(car_info=message.text.strip())
    await state.set_state(DriverRegistration.waiting_for_phone)
    await message.answer(
        "Шаг 3 из 3. Отправьте номер телефона для связи.\n"
        "Нажмите кнопку ниже или введите номер вручную:",
        reply_markup=keyboards.get_phone_keyboard(),
    )


@router.message(DriverRegistration.waiting_for_phone, F.contact)
async def driver_reg_phone_contact(message: Message, state: FSMContext):
    phone = _normalize_phone_digits(message.contact.phone_number or "")
    await _finalize_driver_registration(message, state, phone)


@router.message(DriverRegistration.waiting_for_phone, F.text)
async def driver_reg_phone_text(message: Message, state: FSMContext):
    if message.text and message.text.strip():
        phone = message.text.strip()
        if _is_valid_phone(phone):
            await _finalize_driver_registration(message, state, phone)
            return
    await message.answer(
        "Пожалуйста, введите номер телефона (10–12 цифр). "
        "Можно с +, скобками, дефисами и пробелами.",
    )


async def _finalize_driver_registration(message: Message, state: FSMContext, phone: str):
    """Отправить заявку админу, сохранить в БД, сообщить пользователю."""
    telegram_id = message.from_user.id
    print(f"DEBUG: Saving driver {telegram_id}", flush=True)

    data = await state.get_data()
    print(f"DEBUG DATA: {data}", flush=True)
    full_name = data.get("full_name") or ""
    car_info = data.get("car_info") or ""
    phone = _normalize_phone_digits(phone)

    # Разбиваем "марка госномер" на поля (госномер обычно в конце, буквы/цифры)
    parts = car_info.split()
    car_model = car_info
    car_number = ""
    if len(parts) >= 2:
        last = parts[-1]
        if len(last) >= 5 and any(c.isdigit() for c in last):
            car_number = last
            car_model = " ".join(parts[:-1])
    if not car_number:
        car_number = car_info
        car_model = car_info

    # Прямая запись в БД через TaxiService
    try:
        async with async_session() as db:
            await TaxiService.get_or_create_user(db, telegram_id, name=full_name)
            await TaxiService.update_user_phone(db, telegram_id, phone)
            await TaxiService.register_driver(db, telegram_id, car_model, car_number)
            driver = await TaxiService.get_driver(db, telegram_id)
    except Exception as e:
        await message.answer(f"Ошибка сохранения: {e}")
        logger.error(f"Driver registration save error: {e}", exc_info=True)
        return

    # Сбрасываем FSM только после успешного сохранения
    await state.clear()

    # Уведомление админу о новом водителе
    admin_text = (
        f"📋 Новая заявка на водителя\n\n"
        f"👤 ФИО: {full_name}\n"
        f"🚗 Авто: {car_info}\n"
        f"📱 Телефон: {phone}\n"
        f"🆔 Telegram ID: {telegram_id}"
    )
    try:
        reply_markup = keyboards.get_admin_approval_keyboard(driver.id) if driver else None
        await message.bot.send_message(
            chat_id=settings.ADMIN_CHAT_ID,
            text=admin_text,
            reply_markup=reply_markup,
        )
    except Exception as e:
        logger.error(f"Failed to send driver notification to admin: {e}")

    menu = await _get_menu_for_user(telegram_id)
    await message.answer(
        "✅ Ваша заявка отправлена администратору.\n"
        "Статус: на рассмотрении.",
        reply_markup=menu,
    )


@router.message(F.text == "❌ Уволить водителя")
async def admin_fire_driver_menu_handler(message: Message):
    """
    Кнопка из админского меню: показываем список водителей с кнопками увольнения.
    """
    if not _is_owner(message.from_user.id):
        return
    await admin_list_drivers(message)


@router.message(F.text == "📊 Статистика заказов")
async def admin_stats_handler(message: Message):
    """
    Временный безопасный ответ для кнопки статистики, чтобы она не была "мёртвой".
    """
    if not _is_owner(message.from_user.id):
        return
    await message.answer(
        "📊 Статистика заказов пока недоступна в этом интерфейсе.",
        reply_markup=keyboards.get_admin_menu(),
    )


@router.message(F.text == "💼 Кабинет водителя")
async def driver_cabinet_handler(message: Message):
    """
    Личный кабинет водителя (только для одобренных водителей).
    Кнопка видна только при is_driver=True.
    """
    telegram_id = message.from_user.id
    try:
        async with async_session() as db:
            driver = await TaxiService.get_driver(db, telegram_id)
    except Exception as e:
        logger.error(f"Failed to load driver cabinet for {telegram_id}: {e}", exc_info=True)
        await message.answer(
            "❌ Временно недоступно. Попробуйте позже.",
            reply_markup=await _get_menu_for_user(telegram_id),
        )
        return

    if not driver:
        await message.answer(
            "❌ Не удалось получить данные водителя.",
            reply_markup=await _get_menu_for_user(telegram_id),
        )
        return

    if not driver.is_approved:
        await message.answer(
            "🕓 Ваша заявка на рассмотрении. Дождитесь одобрения.",
            reply_markup=await _get_menu_for_user(telegram_id),
        )
        return

    await message.answer(
        "💼 Кабинет водителя\n\n"
        "Здесь вы можете выйти на смену или приостановить приём заказов.",
        reply_markup=keyboards.get_driver_menu(),
    )


@router.message(F.text == "⚙️ Админка")
async def admin_panel_handler(message: Message):
    """
    Вход в админ-панель. Доступен только для владельца (OWNER_ID).
    """
    if not _is_owner(message.from_user.id):
        # Молча игнорируем или можно вернуть основное меню
        await message.answer(
            "⚠️ У вас нет доступа к админ-панели.",
            reply_markup=await _get_menu_for_user(message.from_user.id),
        )
        return

    await message.answer(
        "⚙️ Админ-панель. Выберите действие:",
        reply_markup=keyboards.get_admin_menu(),
    )


@router.message(F.text == "💎 УПРАВЛЕНИЕ")
async def owner_panel_handler(message: Message):
    """
    Панель владельца (OWNER_ID): отдельное меню управления.
    """
    if not _is_owner(message.from_user.id):
        await message.answer(
            "⚠️ У вас нет доступа к панели владельца.",
            reply_markup=await _get_menu_for_user(message.from_user.id),
        )
        return

    await message.answer(
        "💎 Панель владельца. Выберите действие:",
        reply_markup=keyboards.get_admin_keyboard(),
    )


@router.message(F.text == "👥 Водители в штате")
async def owner_list_drivers(message: Message):
    """
    Список одобренных водителей для владельца: ID | Имя | Авто | Район.
    """
    if not _is_owner(message.from_user.id):
        return

    try:
        resp = await get_http_client().get("/taxi/drivers/all")
    except Exception as e:
        logger.error(f"Failed to load drivers list (owner): {e}")
        await message.answer(
            "❌ Не удалось загрузить список водителей.",
            reply_markup=keyboards.get_admin_keyboard(),
        )
        return

    if resp.status_code != 200:
        await message.answer(
            "❌ Не удалось загрузить список водителей.",
            reply_markup=keyboards.get_admin_keyboard(),
        )
        return

    drivers = resp.json() or []
    if not drivers:
        await message.answer(
            "Список одобренных водителей пуст.",
            reply_markup=keyboards.get_admin_keyboard(),
        )
        return

    lines = ["👥 Водители в штате:\n"]
    for d in drivers:
        name = d.get("name") or "—"
        tg_id = d.get("telegram_id") or "—"
        car_model = d.get("car_model") or "—"
        car_number = d.get("car_number") or "—"
        district = d.get("current_district") or "—"
        lines.append(f"🚖 {name} (ID: {tg_id})\n   {car_model} ({car_number}) | {district}")

    await message.answer(
        "\n".join(lines),
        reply_markup=keyboards.get_admin_keyboard(),
    )


@router.message(F.text == "📩 Новые заявки")
async def owner_pending_drivers(message: Message):
    """
    Список водителей с is_approved=False. Под каждым кнопка «Одобрить».
    """
    if not _is_owner(message.from_user.id):
        return

    try:
        resp = await get_http_client().get("/taxi/admin/drivers/pending")
    except Exception as e:
        logger.error(f"Failed to load pending drivers: {e}")
        await message.answer(
            "❌ Не удалось загрузить список заявок.",
            reply_markup=keyboards.get_admin_keyboard(),
        )
        return

    if resp.status_code != 200:
        await message.answer(
            "❌ Не удалось загрузить список заявок.",
            reply_markup=keyboards.get_admin_keyboard(),
        )
        return

    drivers = resp.json() or []
    if not drivers:
        await message.answer(
            "📩 Нет новых заявок на одобрение.",
            reply_markup=keyboards.get_admin_keyboard(),
        )
        return

    await message.answer(
        "📩 Новые заявки на одобрение:",
        reply_markup=keyboards.get_admin_keyboard(),
    )
    for d in drivers:
        name = d.get("name") or "—"
        car_model = d.get("car_model") or "—"
        car_number = d.get("car_number") or "—"
        text = (
            f"👤 {name}\n"
            f"🚗 {car_model} ({car_number})"
        )
        kb = keyboards.get_admin_approval_keyboard(d.get("id"))
        await message.answer(text, reply_markup=kb)


@router.message(F.text == "🔙 Назад")
async def owner_back_to_menu(message: Message, state: FSMContext):
    """
    Возврат в главное меню из панели владельца/админки.
    Приоритетный хендлер — срабатывает в любом состоянии (в т.ч. при вводе ID водителя).
    """
    if not _is_owner(message.from_user.id):
        return

    await state.clear()
    await message.answer(
        "Главное меню:",
        reply_markup=await _get_menu_for_user(message.from_user.id),
    )


@router.message(F.text == "❌ Удалить водителя (по ID)")
async def owner_delete_driver_start(message: Message, state: FSMContext):
    """
    Запрос ID водителя (telegram_id) для удаления.
    """
    if not _is_owner(message.from_user.id):
        return

    await state.set_state(OwnerDeleteDriver.waiting_for_driver_id)
    await message.answer(
        "Введите ID водителя (telegram_id), которого нужно удалить:",
        reply_markup=keyboards.get_admin_keyboard(),
    )


@router.message(OwnerDeleteDriver.waiting_for_driver_id, F.text)
async def owner_delete_driver_process_id(message: Message, state: FSMContext):
    """
    Обработка введённого ID: запрос к API, показ подтверждения.
    """
    if not _is_owner(message.from_user.id):
        return

    # Если нажали «Назад» или «Отмена» — не ищем водителя, возвращаем в админ-меню
    if message.text and message.text.strip() in ("🔙 Назад", "❌ Отмена"):
        await state.clear()
        await message.answer(
            "💎 Панель владельца. Выберите действие:",
            reply_markup=keyboards.get_admin_keyboard(),
        )
        return

    raw = (message.text or "").strip()
    if not raw or not raw.isdigit():
        await message.answer(
            "⚠️ Ошибка: ID должен состоять только из цифр.",
            reply_markup=keyboards.get_admin_keyboard(),
        )
        return

    tg_id = int(raw)
    try:
        resp = await get_http_client().get(f"/taxi/admin/driver/{tg_id}/confirm_info")
    except Exception as e:
        logger.exception(f"Failed to fetch driver confirm info for tg_id={tg_id}: {e}")
        await state.clear()
        await message.answer(
            "❌ Не удалось получить данные водителя.",
            reply_markup=keyboards.get_admin_keyboard(),
        )
        return

    if resp.status_code == 404:
        await message.answer(
            f"❌ Водитель с ID {tg_id} не найден в базе.",
            reply_markup=keyboards.get_admin_keyboard(),
        )
        return

    if resp.status_code != 200:
        await state.clear()
        await message.answer(
            "❌ Ошибка при получении данных водителя.",
            reply_markup=keyboards.get_admin_keyboard(),
        )
        return

    data = resp.json()
    await state.clear()

    name = data.get("name") or "—"
    car_model = data.get("car_model") or "—"
    car_number = data.get("car_number") or "—"
    car_str = f"{car_model} ({car_number})"

    text = (
        "❓ Подтвердите удаление водителя:\n\n"
        f"👤 Имя: {name}\n"
        f"🆔 ID: {tg_id}\n"
        f"🚗 Авто: {car_str}"
    )
    await message.answer(
        text,
        reply_markup=keyboards.get_confirm_delete_keyboard(tg_id),
    )


@router.message(F.text == "✅ Одобрить новичков")
async def admin_approve_novices(message: Message):
    """
    Показ списка новых заявок для одобрения (из меню ⚙️ Админка).
    """
    if not _is_owner(message.from_user.id):
        return
    # Используем ту же логику, что и «📩 Новые заявки»
    await owner_pending_drivers(message)


@router.message(F.text == "👥 Список водителей")
async def admin_list_drivers(message: Message):
    """
    Список водителей для администратора: Имя | Машина | Статус.
    """
    if not _is_owner(message.from_user.id):
        return

    try:
        resp = await get_http_client().get("/taxi/drivers/all")
    except Exception as e:
        logger.error(f"Failed to load drivers list: {e}")
        await message.answer(
            "❌ Не удалось загрузить список водителей.",
            reply_markup=keyboards.get_admin_menu(),
        )
        return

    if resp.status_code != 200:
        await message.answer(
            "❌ Не удалось загрузить список водителей.",
            reply_markup=keyboards.get_admin_menu(),
        )
        return

    drivers = resp.json() or []
    if not drivers:
        await message.answer(
            "Список водителей пуст.",
            reply_markup=keyboards.get_admin_menu(),
        )
        return

    await message.answer(
        "👥 Список одобренных водителей:",
        reply_markup=keyboards.get_admin_menu(),
    )
    for d in drivers:
        status = "Работает" if d.get("current_district") else "Нет"
        name = d.get("name") or "—"
        car_model = d.get("car_model") or "—"
        car_number = d.get("car_number") or "—"
        tg_id = d.get("telegram_id")
        if not tg_id:
            continue
        text = (
            f"🚖 {name} (ID: {tg_id})\n"
            f"🚗 {car_model} ({car_number})\n"
            f"📍 Статус: {status}"
        )
        kb = InlineKeyboardBuilder()
        kb.button(
            text="❌ Уволить",
            callback_data=f"fire_driver_{tg_id}",
        )
        await message.answer(text, reply_markup=kb.as_markup())


@router.message(F.text == "▶️ Выйти на смену")
async def driver_go_online(message: Message, state: FSMContext):
    """
    Водитель выходит на смену: сначала просим выбрать район,
    затем помечаем его как онлайн и сохраняем район.
    """
    await state.set_state(DriverShift.waiting_for_district)
    await message.answer(
        "Выберите вашу текущую локацию (стоянку):",
        reply_markup=keyboards.get_driver_districts_keyboard(),
    )


@router.message(F.text == "⏸ Уйти со смены")
async def driver_go_offline(message: Message):
    """Водитель уходит со смены: убираем его из локального списка онлайн-водителей."""
    online_drivers.discard(message.from_user.id)
    await message.answer(
        "⏸ Вы ушли со смены. Новые заказы больше не будут приходить в этот чат.",
        reply_markup=keyboards.get_driver_menu(),
    )


@router.message(DriverShift.waiting_for_district, F.text)
async def driver_select_district(message: Message, state: FSMContext):
    """
    Водитель выбирает район. Сохраняем район в бэкенд и отмечаем водителя онлайн.
    """
    text = message.text
    allowed = {
        "📍 Губашево": "Губашево",
        "📍 Проспект": "Проспект",
        "📍 30-й": "30-й",
        "📍 Центр": "Центр",
        "📍 Луч": "Луч",
        "📍 Берсол": "Берсол",
        "📍 Владимир": "Владимир",
        "📍 Титовка (Начало)": "Титовка (Начало)",
        "📍 Титовка (Конец)": "Титовка (Конец)",
        "📍 Садовка": "Садовка",
        "📍 Нагорный": "Нагорный",
        "📍 Озон": "Озон",
    }
    district = allowed.get(text)
    if not district:
        await message.answer(
            "Пожалуйста, выберите стоянку из списка кнопок ниже.",
            reply_markup=keyboards.get_driver_districts_keyboard(),
        )
        return

    try:
        resp = await get_http_client().post(
            "/taxi/driver/set_district",
            json={
                "telegram_id": message.from_user.id,
                "district": district,
            },
        )
        if resp.status_code != 200:
            await message.answer(
                "❌ Не удалось сохранить район. Попробуйте позже.",
                reply_markup=keyboards.get_driver_menu(),
            )
            return
    except Exception as e:
        logger.error(f"Failed to set district for driver {message.from_user.id}: {e}")
        await message.answer(
            "❌ Ошибка при сохранении района. Попробуйте позже.",
            reply_markup=keyboards.get_driver_menu(),
        )
        return

    online_drivers.add(message.from_user.id)
    await state.clear()
    await message.answer(
        f"✅ Вы встали в очередь в районе {district}. Ожидайте заказов.",
        reply_markup=keyboards.get_driver_menu(),
    )


@router.callback_query(F.data == "back_to_menu")
async def back_to_menu_callback(callback: CallbackQuery, state: FSMContext):
    """Возврат в главное меню из списка заказов или других экранов."""
    await state.clear()
    await callback.answer()
    await callback.message.answer(
        "Главное меню. Выберите действие:",
        reply_markup=await _get_menu_for_user(callback.from_user.id),
    )


@router.callback_query(F.data == "manual_from", OrderTaxi.waiting_for_from_address)
async def manual_from_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    sent = await callback.message.answer(
        "Напишите адрес улицы и номер дома (откуда забрать):",
        reply_markup=ReplyKeyboardRemove(),
    )
    data = await state.get_data()
    msg_list = data.get("msg_to_delete", [])
    msg_list.append(sent.message_id)
    await state.update_data(msg_to_delete=msg_list)


@router.callback_query(F.data == "manual_to", OrderTaxi.waiting_for_to_address)
async def manual_to_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    destination_addresses = data.get("destination_addresses", [])
    from_address = data.get("from_address", "—")
    if destination_addresses:
        text = (
            f"{_format_route_vertical(from_address, destination_addresses)}\n\n"
            "Напишите адрес следующей остановки:"
        )
    else:
        text = "Напишите адрес улицы и номер дома (куда едем):"
    try:
        await callback.message.edit_text(text=text, reply_markup=None)
    except Exception:
        pass
    await state.update_data(
        msg_to_delete=[callback.message.message_id],
        route_message_id=callback.message.message_id,
    )


@router.callback_query(F.data.startswith("recent_from_"), OrderTaxi.waiting_for_from_address)
async def recent_from_callback(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    recent_addresses = data.get("recent_from_addresses", [])
    idx = int(callback.data.split("_")[-1])
    if idx < 0 or idx >= len(recent_addresses):
        await callback.answer("Адрес больше недоступен. Выберите заново.", show_alert=True)
        return
    from_address = recent_addresses[idx]
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    msg_list = data.get("msg_to_delete", [])
    msg_list.append(callback.message.message_id)
    await state.update_data(
        msg_to_delete=msg_list,
        from_address=from_address,
    )
    print(f"DEBUG ORDER: selected recent from_address='{from_address}' for user {callback.from_user.id}", flush=True)
    await _delete_messages(callback.bot, callback.message.chat.id, msg_list)
    await state.update_data(msg_to_delete=[])
    await _prompt_for_to_address(callback.message, state, callback.from_user.id)


@router.callback_query(F.data.startswith("recent_to_"), OrderTaxi.waiting_for_to_address)
async def recent_to_callback(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    recent_addresses = data.get("recent_to_addresses", [])
    idx = int(callback.data.split("_")[-1])
    if idx < 0 or idx >= len(recent_addresses):
        await callback.answer("Адрес больше недоступен. Выберите заново.", show_alert=True)
        return
    to_address = recent_addresses[idx]
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _save_destination_and_show_options(
        callback.message,
        state,
        to_address,
        callback.from_user.id,
        edit_message_id=callback.message.message_id,
    )


@router.callback_query(F.data == "add_more_address", OrderTaxi.waiting_for_to_address)
async def add_more_address_callback(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    from_address = data.get("from_address", "—")
    destination_addresses = data.get("destination_addresses", [])
    if not destination_addresses:
        await callback.answer("Сначала добавьте адрес назначения.", show_alert=True)
        return
    await callback.answer()
    recent_addresses = await _get_recent_addresses(callback.from_user.id, "to")
    text = _build_route_with_add_more_prompt(from_address, destination_addresses)
    keyboard = _build_recent_addresses_keyboard(recent_addresses, "to")
    try:
        await callback.message.edit_text(
            text=text,
            reply_markup=keyboard,
        )
        route_msg_id = callback.message.message_id
    except Exception:
        sent = await callback.message.answer(text, reply_markup=keyboard)
        route_msg_id = sent.message_id
    await state.update_data(
        msg_to_delete=[route_msg_id],
        route_message_id=route_msg_id,
        recent_to_addresses=recent_addresses,
    )


@router.callback_query(F.data == "finish_route", OrderTaxi.waiting_for_to_address)
async def finish_route_callback(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    destination_addresses = data.get("destination_addresses", [])
    if not destination_addresses:
        await callback.answer("Сначала добавьте хотя бы один адрес назначения.", show_alert=True)
        return
    await callback.answer()
    # Удаляем сообщение с кнопкой «Далее», чтобы не засорять чат
    try:
        await callback.message.delete()
    except Exception:
        pass
    await state.update_data(
        to_address="\n".join(destination_addresses),
        has_child_seat=bool(data.get("has_child_seat", False)),
        has_pet=bool(data.get("has_pet", False)),
        order_comment=data.get("order_comment"),
    )
    await _show_order_options_screen(callback.message, state)


@router.callback_query(F.data == "toggle_child_seat", OrderTaxi.waiting_for_options)
async def toggle_child_seat_callback(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.update_data(has_child_seat=not bool(data.get("has_child_seat")))
    await callback.answer("Опция обновлена")
    try:
        await callback.message.edit_text(
            _build_order_options_text(
                data.get("from_address") or "—",
                data.get("destination_addresses") or [],
                data.get("order_comment"),
            ),
            reply_markup=keyboards.get_order_options_keyboard(
                has_child_seat=not bool(data.get("has_child_seat")),
                has_pet=bool(data.get("has_pet")),
            ),
        )
    except Exception:
        pass


@router.callback_query(F.data == "toggle_pet", OrderTaxi.waiting_for_options)
async def toggle_pet_callback(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.update_data(has_pet=not bool(data.get("has_pet")))
    await callback.answer("Опция обновлена")
    try:
        await callback.message.edit_text(
            _build_order_options_text(
                data.get("from_address") or "—",
                data.get("destination_addresses") or [],
                data.get("order_comment"),
            ),
            reply_markup=keyboards.get_order_options_keyboard(
                has_child_seat=bool(data.get("has_child_seat")),
                has_pet=not bool(data.get("has_pet")),
            ),
        )
    except Exception:
        pass


@router.callback_query(F.data == "add_order_comment", OrderTaxi.waiting_for_options)
async def add_order_comment_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    sent = await callback.message.answer(
        "✍️ Напишите комментарий для водителя.\nНапример: Подъезд 2.",
        reply_markup=ReplyKeyboardRemove(),
    )
    data = await state.get_data()
    msg_list = data.get("msg_to_delete", [])
    msg_list.append(sent.message_id)
    await state.update_data(msg_to_delete=msg_list)
    await state.set_state(OrderTaxi.waiting_for_comment)


@router.callback_query(F.data == "calculate_order_price", OrderTaxi.waiting_for_options)
async def calculate_order_price_callback(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    destination_addresses = data.get("destination_addresses") or []
    if not destination_addresses:
        await callback.answer("Сначала добавьте хотя бы один адрес назначения.", show_alert=True)
        return
    calculated_price, price_note = _estimate_order_price(data)
    await state.update_data(calculated_price=calculated_price, price_note=price_note)
    summary_data = {**data, "calculated_price": calculated_price, "price_note": price_note}
    await callback.answer()
    summary_message_id = callback.message.message_id
    try:
        await callback.message.edit_text(
            _build_final_summary_text(summary_data),
            reply_markup=keyboards.get_order_confirmation_keyboard(),
        )
    except Exception:
        sent = await callback.message.answer(
            _build_final_summary_text(summary_data),
            reply_markup=keyboards.get_order_confirmation_keyboard(),
        )
        summary_message_id = sent.message_id
    await state.update_data(msg_to_delete=[summary_message_id])
    await state.set_state(OrderTaxi.waiting_for_confirmation)


@router.callback_query(F.data == "confirm_order_creation", OrderTaxi.waiting_for_confirmation)
async def confirm_order_creation_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await finalize_order(callback.message, state, requester_telegram_id=callback.from_user.id)


@router.callback_query(F.data == "cancel_order_creation", OrderTaxi.waiting_for_options)
@router.callback_query(F.data == "cancel_order_creation", OrderTaxi.waiting_for_confirmation)
async def cancel_order_creation_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    prev_cancel_id = data.get("last_cancel_msg_id")
    await state.clear()
    # Удаляем сообщение с итогом/опциями
    try:
        await callback.message.delete()
    except Exception:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    if prev_cancel_id:
        try:
            await callback.bot.delete_message(
                chat_id=callback.message.chat.id,
                message_id=prev_cancel_id,
            )
        except Exception:
            pass
    sent = await callback.message.answer(
        "Заказ отменен. Вы можете начать новый заказ в любое время.",
        reply_markup=keyboards.get_start_order_inline_keyboard(),
    )
    await state.update_data(last_cancel_msg_id=sent.message_id)


async def finalize_order(
    message: Message,
    state: FSMContext,
    comment: str | None = None,
    requester_telegram_id: int | None = None,
):
    # Работаем только после явного подтверждения заказа
    current_state = await state.get_state() or ""
    if current_state != OrderTaxi.waiting_for_confirmation.state:
        return
    data = await state.get_data()
    # Защита от двойного вызова: один клик = один заказ
    if data.get("is_processing"):
        return
    # Заказ в чат водителей отправляем только если поток запущен с кнопки «Заказать такси».
    if not data.get("order_started_by_button"):
        logger.warning(
            "finalize_order: order_started_by_button missing for user %s, refusing to create order",
            message.from_user.id,
        )
        await state.clear()
        await message.answer(
            "Пожалуйста, используйте кнопку «🚕 Заказать такси» в меню для оформления заказа.",
            reply_markup=await _get_menu_for_user(message.from_user.id),
        )
        return

    from_address = data.get('from_address')
    destination_addresses = data.get("destination_addresses") or []
    to_address = data.get('to_address') or "\n".join(destination_addresses)
    
    if not from_address or not destination_addresses:
        await message.answer("❌ Ошибка: адрес отправления или назначения не указан.")
        await state.clear()
        return

    route_display = "\n".join(destination_addresses)
    final_comment = _build_order_comment_payload(data, explicit_comment=comment)
    calculated_price = data.get("calculated_price")
    route_vertical = _format_route_vertical(from_address, destination_addresses)
    passenger_telegram_id = requester_telegram_id or data.get("requester_telegram_id") or message.chat.id

    await state.update_data(is_processing=True)

    try:
        response = await get_http_client().post(
            "/taxi/order",
            json={
                "telegram_id": passenger_telegram_id,
                "from_address": from_address,
                "to_address": route_display,
                "comment": final_comment,
                "price": calculated_price,
            }
        )
            
        if response.status_code == 200:
            order = response.json()
            order_id = order['id']
            await state.update_data(order_id=order_id)
            base_text = (
                "✅ Заказ создан! Ищем водителя...\n\n"
                f"{route_vertical}"
            )
            if isinstance(calculated_price, (int, float)):
                base_text += f"\n\n💰 Стоимость: {calculated_price:.0f} руб."
            if final_comment:
                base_text += f"\n\n💬 Примечание: {final_comment}"

            sent_msg = await message.answer(
                base_text,
                reply_markup=keyboards.get_order_manage_keyboard(order_id)
            )

            data = await state.get_data()
            msg_list = data.get("msg_to_delete", [])
            msg_list.append(sent_msg.message_id)
            await state.update_data(
                msg_to_delete=msg_list,
                active_message_id=sent_msg.message_id,
            )
            
            # Отправка в чат водителей
            driver_msg = (
                f"🚕 **Новый заказ #{order_id}**\n\n"
                f"{route_vertical}"
            )
            if isinstance(calculated_price, (int, float)):
                driver_msg += f"\n\n💰 Стоимость: {calculated_price:.0f} руб."
            if final_comment:
                driver_msg += f"\n\n💬 Комментарий: {final_comment}"
            
            try:
                await message.bot.send_message(
                    settings.DRIVER_CHAT_ID,
                    driver_msg,
                    reply_markup=keyboards.get_accept_order_keyboard(order_id)
                )
                # Дублируем заказ всем водителям, которые вышли на смену, в личные сообщения
                for driver_tg_id in list(online_drivers):
                    try:
                        await message.bot.send_message(
                            chat_id=driver_tg_id,
                            text=driver_msg,
                            reply_markup=keyboards.get_accept_order_keyboard(order_id),
                        )
                    except Exception as e:
                        logger.error(f"Не удалось отправить заказ водителю {driver_tg_id}: {e}")
            except Exception as e:
                logger.error(f"Ошибка отправки в чат водителей ({settings.DRIVER_CHAT_ID}): {e}")
            # Снимаем состояние FSM, но оставляем order_id в data для мягкой отмены/обработки UI
            await state.set_state(None)
        else:
            error_detail = response.text
            logger.error(f"Ошибка API (Order): {response.status_code} - {error_detail}")
            print(f"[API] Order 500/error: {response.status_code} - {error_detail}", flush=True)
            await message.answer(f"❌ Ошибка на стороне сервера (Status {response.status_code}). Попробуйте позже.")
            await state.clear()
                
    except httpx.ConnectError as e:
        logger.error(f"Connection error to {settings.API_BASE_URL}: {e}")
        print(f"[API] ConnectionError: {e}", flush=True)
        await message.answer("❌ Не удалось связаться с сервером. Убедитесь, что Бэкенд запущен.")
        await state.clear()
    except httpx.TimeoutException as e:
        logger.error(f"Timeout talking to {settings.API_BASE_URL}: {e}")
        print(f"[API] Timeout: {e}", flush=True)
        await message.answer("❌ Сервер не отвечает вовремя. Попробуйте позже.")
        await state.clear()
    except Exception as e:
        logger.exception(f"Критическая ошибка при заказе: {e}")
        import traceback
        print(f"[API] Order exception: {e}\n{traceback.format_exc()}", flush=True)
        await message.answer(f"❌ Произошла непредвиденная ошибка: {e}")
        await state.clear()

@router.callback_query(F.data.startswith("accept_"))
async def accept_order_callback(callback: CallbackQuery, state: FSMContext):
    """
    Первый шаг: водитель нажал «Принять заказ».
    Показываем выбор времени прибытия, НЕ вызываем API.
    """
    order_id = int(callback.data.split("_")[-1])
    await callback.answer()
    try:
        await callback.message.edit_text(
            f"🚕 Заказ #{order_id}\n\n"
            "Через сколько минут вы будете на месте?",
            reply_markup=keyboards.get_eta_select_keyboard(order_id),
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("eta_"))
async def eta_select_callback(callback: CallbackQuery, state: FSMContext):
    """
    Второй шаг: водитель выбрал время прибытия.
    Вызываем API accept, отправляем данные водителю и пассажиру.
    """
    # callback.data: eta_{interval}_{order_id}, например eta_1-3_123
    parts = callback.data.split("_", 2)
    eta_key = parts[1]  # "1-3", "4-6", "7-10" и т.д.
    order_id = int(parts[2])
    eta_display = _eta_key_to_display(eta_key)
    driver_telegram_id = callback.from_user.id

    try:
        response = await get_http_client().post(
            "/taxi/accept",
            json={
                "order_id": order_id,
                "driver_telegram_id": driver_telegram_id,
            },
        )

        if response.status_code == 200:
            order = response.json()

            await callback.answer("Вы приняли заказ! ✅")
            try:
                await callback.message.edit_text(
                    f"🚕 Заказ #{order_id} принят водителем {callback.from_user.full_name}.",
                    reply_markup=None,
                )
            except Exception:
                pass

            # Карточка заказа в личку водителю (адрес, телефон)
            try:
                route_text = _format_route_from_values(order.get("from_address"), order.get("to_address"))
                card_text = (
                    f"🚕 **Вы приняли заказ #{order_id}**\n\n"
                    f"📍 Адрес: {order.get('from_address', '—')}\n"
                    f"👤 Телефон клиента: {order.get('client_phone', 'не указан')}\n\n"
                    f"{route_text}"
                    + (f"\n💬 Примечание: {order.get('comment')}" if order.get('comment') else "\n💬 Примечание: Нет")
                )
                await callback.bot.send_message(
                    chat_id=driver_telegram_id,
                    text=card_text,
                    reply_markup=keyboards.get_driver_accept_keyboard(
                        order_id,
                        order["from_address"],
                        order.get("client_phone"),
                    ),
                )
            except Exception as e:
                logger.exception(f"Error sending private order card to driver {driver_telegram_id}: {e}")

            # Уведомление пассажиру: EDIT активного сообщения (State-based UI)
            client_chat_id = order.get("client_telegram_id")
            try:
                me = await callback.bot.get_me()
                if client_chat_id == me.id:
                    logger.warning("Попытка отправить сообщение самому боту (client_telegram_id=%s)", client_chat_id)
                elif client_chat_id:
                    from_addr = order.get("from_address") or ""
                    to_addr = order.get("to_address") or ""
                    ride_mins = get_ride_minutes(from_addr, to_addr)
                    route_text = _format_route_from_values(from_addr, to_addr)
                    driver_name = callback.from_user.full_name or "Водитель"
                    text = (
                        f"🚖 Водитель {driver_name} принял ваш заказ!\n"
                        f"Он будет у вас в течение {eta_display}.\n\n"
                        f"{route_text}\n\n"
                        f"🚗 Машина: {order.get('car_model', '—')}\n"
                        f"🔢 Номер: {order.get('car_number', '—')}\n\n"
                        f"⏱ Время в пути — ~{ride_mins} мин."
                    )
                    logger.info("Sending accept notification to passenger %s for order %s", client_chat_id, order_id)
                    driver_tg_id = callback.from_user.id
                    p_state = _get_passenger_state(callback.bot, state.storage, client_chat_id)
                    pdata = await p_state.get_data()
                    active_id = pdata.get("active_message_id")
                    edited = False
                    if isinstance(active_id, int):
                        try:
                            await callback.bot.edit_message_text(
                                chat_id=client_chat_id,
                                message_id=active_id,
                                text=text,
                                reply_markup=keyboards.get_client_after_accept_keyboard(order_id, driver_tg_id),
                            )
                            edited = True
                            await p_state.update_data(msg_to_delete=[active_id])
                        except Exception:
                            pass
                    if not edited:
                        if isinstance(active_id, int):
                            try:
                                await callback.bot.delete_message(chat_id=client_chat_id, message_id=active_id)
                            except Exception:
                                pass
                        sent_msg = await callback.bot.send_message(
                            client_chat_id,
                            text,
                            reply_markup=keyboards.get_client_after_accept_keyboard(order_id, driver_tg_id),
                        )
                        await p_state.update_data(
                            active_message_id=sent_msg.message_id,
                            msg_to_delete=[sent_msg.message_id],
                        )
            except Exception as e:
                logger.exception(f"Error sending message to client {client_chat_id}: {e}")
        else:
            # Попробуем показать полезное сообщение из FastAPI (поле detail)
            detail_msg = None
            try:
                data = response.json()
                if isinstance(data, dict):
                    detail_msg = data.get("detail")
            except Exception:
                detail_msg = None

            if not isinstance(detail_msg, str) or not detail_msg:
                detail_msg = "Ошибка: заказ уже принят, не найден или у вас нет прав принять его ❌"

            # Логируем ответ бэкенда для упрощения отладки
            logger.error(
                "Ошибка принятия заказа: %s %s",
                response.status_code,
                response.text,
            )
            await callback.answer(detail_msg, show_alert=True)
    except Exception as e:
        logger.exception(f"Error in eta_select_callback: {e}")
        await callback.answer("❌ Ошибка при принятии заказа", show_alert=True)


@router.callback_query(F.data.startswith("complete_"))
async def complete_order_callback(callback: CallbackQuery, state: FSMContext):
    # callback.data имеет вид "complete_{order_id}"
    order_id = int(callback.data.split("_")[-1])
    
    try:
        # Сначала пробуем удалить само сообщение с кнопкой "Завершить", если оно ещё существует
        try:
            await callback.message.delete()
        except Exception:
            pass

        # Пытаемся удалить предыдущее уведомление "клиент выходит", если оно было
        try:
            data = await state.get_data()
            out_msg_id = data.get("last_out_msg_id") or data.get("last_out_notification_id")
            if isinstance(out_msg_id, int):
                try:
                    await callback.bot.delete_message(
                        chat_id=callback.from_user.id,
                        message_id=out_msg_id,
                    )
                except Exception:
                    pass
        except Exception:
            pass

        response = await get_http_client().post(f"/taxi/order/{order_id}/complete")
        
        if response.status_code == 200:
            data = response.json()
            client_chat_id = data.get("client_telegram_id")

            # State-based UI: EDIT активного сообщения → чек с оценкой (финализация)
            if client_chat_id is not None:
                try:
                    me = await callback.bot.get_me()
                    if client_chat_id == me.id:
                        logger.warning("Ошибка: попытка отправить сообщение боту")
                        return
                    p_state = _get_passenger_state(callback.bot, state.storage, client_chat_id)
                    p_data = await p_state.get_data()
                    ord_data = data

                    if not ord_data:
                        logger.error(f"ORD_DATA пустой при завершении заказа {order_id}")

                    driver_name = ord_data.get("driver_name", "R S")
                    car_model = ord_data.get("car_model", "Гранта")
                    car_number = ord_data.get("car_number", "255")
                    route_text = _format_route_from_values(ord_data.get("from_address"), ord_data.get("to_address"))

                    receipt_text = (
                        "🏁 Поездка завершена. Спасибо, что вы с нами!\n\n"
                        f"{route_text}\n\n"
                        f"🚖 Ваш заказ выполнил: {driver_name}\n"
                        f"🚗 Машина: {car_model}\n"
                        f"🔢 Номер: {car_number}\n\n"
                        "Пожалуйста, оцените работу водителя:"
                    )

                    active_id = p_data.get("active_message_id")
                    edited = False
                    if isinstance(active_id, int):
                        try:
                            await callback.bot.edit_message_text(
                                chat_id=client_chat_id,
                                message_id=active_id,
                                text=receipt_text,
                                reply_markup=keyboards.get_rate_trip_keyboard(order_id),
                            )
                            edited = True
                        except Exception:
                            pass
                    if not edited:
                        if isinstance(active_id, int):
                            try:
                                await callback.bot.delete_message(chat_id=client_chat_id, message_id=active_id)
                            except Exception:
                                pass
                        receipt_msg = await callback.bot.send_message(
                            chat_id=client_chat_id,
                            text=receipt_text,
                            reply_markup=keyboards.get_rate_trip_keyboard(order_id),
                        )
                        active_id = receipt_msg.message_id

                    logger.info("Sending complete notification to passenger %s for order %s", client_chat_id, order_id)

                    new_order_prompt = await callback.bot.send_message(
                        chat_id=client_chat_id,
                        text="🚖 Нажмите кнопку ниже, чтобы заказать такси:",
                        reply_markup=keyboards.get_new_order_after_rating_keyboard(),
                    )
                    to_del = p_data.get("msg_to_delete", [])
                    to_del.append(new_order_prompt.message_id)
                    await p_state.update_data(
                        msg_to_delete=to_del,
                        last_new_order_prompt_id=new_order_prompt.message_id,
                    )
                except Exception as e:
                    logger.exception(f"Error sending 'trip completed' (receipt) message to client {client_chat_id}: {e}")

            await callback.answer("Заказ завершен! Отличная работа 👍")
            try:
                await callback.message.delete()
            except Exception:
                pass
            price = data.get("price")
            price_str = f"{price:.0f} руб." if price is not None else "—"
            await callback.bot.send_message(
                callback.from_user.id,
                f"✅ Заказ №{order_id} успешно завершен!\n💰 Сумма к оплате: {price_str}",
            )

            # После завершения заказа просим водителя указать актуальную стоянку
            await state.set_state(DriverShift.waiting_for_district)
            await callback.bot.send_message(
                chat_id=callback.from_user.id,
                text="Выберите вашу текущую локацию (стоянку):",
                reply_markup=keyboards.get_driver_districts_keyboard(),
            )
        else:
            # Попробуем достать detail
            detail_msg = None
            try:
                payload = response.json()
                if isinstance(payload, dict):
                    detail_msg = payload.get("detail")
            except Exception:
                detail_msg = None
            if not isinstance(detail_msg, str) or not detail_msg:
                detail_msg = "Ошибка при завершении заказа"

            logger.error("Ошибка complete_order: %s %s", response.status_code, response.text)
            await callback.answer(detail_msg, show_alert=True)
    except Exception as e:
        logger.exception(f"Error in complete_order_callback: {e}")
        await callback.answer("❌ Ошибка при завершении заказа", show_alert=True)

@router.callback_query(F.data == "cancel_delete")
async def cancel_delete_driver_callback(callback: CallbackQuery):
    """
    Отмена удаления водителя.
    """
    if not _is_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    try:
        await callback.message.edit_text("Сброшено. Водитель не удален")
    except Exception:
        pass
    await callback.answer("Отменено", show_alert=False)
    await callback.message.answer(
        "💎 Панель владельца. Выберите действие:",
        reply_markup=keyboards.get_admin_keyboard(),
    )


@router.callback_query(F.data.startswith("cancel_"))
async def cancel_order_callback(callback: CallbackQuery, state: FSMContext):
    # callback.data имеет вид "cancel_{order_id}" (не cancel_delete)
    data = await state.get_data()
    state_order_id = data.get("order_id")
    raw_order_id = callback.data.split("_")[-1]
    if state_order_id is None and not raw_order_id.isdigit():
        await callback.answer()
        await state.clear()
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.message.answer(
            "❌ Заказ отменен",
            reply_markup=await _get_menu_for_user(callback.from_user.id),
        )
        return

    order_id = int(state_order_id) if state_order_id is not None else int(raw_order_id)
    
    try:
        response = await get_http_client().post(
            f"/taxi/order/{order_id}/cancel",
            params={"telegram_id": callback.from_user.id}
        )
        
        if response.status_code == 200:
            await state.clear()
            await callback.answer("Заказ отменен ❌")
            try:
                await callback.message.edit_text(
                    f"❌ Заказ #{order_id} был отменен вами."
                )
            except Exception:
                pass
        else:
            if state_order_id is None:
                await state.clear()
                try:
                    await callback.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    pass
                await callback.message.answer(
                    "❌ Заказ отменен",
                    reply_markup=await _get_menu_for_user(callback.from_user.id),
                )
                await callback.answer()
                return
            await callback.answer("Ошибка при отмене: заказ уже принят или не найден", show_alert=True)
    except Exception as e:
        logger.exception(f"Error in cancel_order_callback: {e}")
        await callback.answer("❌ Ошибка при отмене заказа", show_alert=True)
@router.callback_query(F.data.startswith("approve_"))
async def approve_driver_callback(callback: CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    driver_id = int(callback.data.split("_")[1])
    try:
        response = await get_http_client().post(f"/taxi/driver/{driver_id}/approve")
        if response.status_code == 200:
            data = response.json()
            await callback.bot.send_message(
                data['telegram_id'],
                "Поздравляем! Ваша заявка одобрена. Теперь вам доступен Кабинет водителя.",
                reply_markup=await _get_menu_for_user(data['telegram_id']),
            )
            try:
                await callback.message.edit_text(callback.message.text + "\n\n✅ Одобрен")
            except Exception:
                pass
            await callback.answer("Заявка одобрена", show_alert=False)
        else:
            await callback.answer("Ошибка при одобрении", show_alert=True)
    except Exception as e:
        logger.exception(f"Error in approve_driver_callback: {e}")
        await callback.answer("❌ Ошибка при одобрении водителя", show_alert=True)

@router.callback_query(F.data.startswith("reject_"))
async def reject_driver_callback(callback: CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    driver_id = int(callback.data.split("_")[1])
    try:
        response = await get_http_client().post(f"/taxi/driver/{driver_id}/reject")
        if response.status_code == 200:
            data = response.json()
            await callback.bot.send_message(
                data['telegram_id'],
                "❌ Ваша заявка на регистрацию водителем отклонена."
            )
            try:
                await callback.message.edit_text(callback.message.text + "\n\n❌ Отклонен")
            except Exception:
                pass
        else:
            await callback.answer("Ошибка при отклонении", show_alert=True)
    except Exception as e:
        logger.exception(f"Error in reject_driver_callback: {e}")
        await callback.answer("❌ Ошибка при отклонении водителя", show_alert=True)


@router.callback_query(F.data.startswith("confirm_delete_"))
async def confirm_delete_driver_callback(callback: CallbackQuery):
    """
    Подтверждение удаления водителя: «Да, удалить».
    """
    if not _is_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    tg_id = int(callback.data.split("_")[-1])
    try:
        resp = await get_http_client().delete(f"/taxi/admin/driver/{tg_id}")
        if resp.status_code == 200:
            data = resp.json()
            driver_name = data.get("name") or "—"
            try:
                await callback.bot.send_message(
                    data["telegram_id"],
                    "❌ Вы были удалены из списка водителей CHVK City.",
                )
            except Exception:
                pass
            try:
                await callback.message.edit_text(
                    f"✅ Водитель {driver_name} (ID: {tg_id}) успешно удален из системы"
                )
            except Exception:
                pass
            await callback.answer("Водитель удалён", show_alert=False)
        elif resp.status_code == 404:
            await callback.answer(f"❌ Водитель с ID {tg_id} не найден в базе.", show_alert=True)
        else:
            await callback.answer("Ошибка при удалении водителя", show_alert=True)
    except Exception as e:
        logger.exception(f"Error in confirm_delete_driver_callback (tg_id={tg_id}): {e}")
        await callback.answer("❌ Ошибка при удалении водителя", show_alert=True)


@router.callback_query(F.data.startswith("fire_driver_"))
async def fire_driver_callback(callback: CallbackQuery):
    """
    Увольнение/удаление водителя из админ-панели.
    """
    if not _is_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    driver_tg_id = int(callback.data.split("_")[-1])
    try:
        response = await get_http_client().delete(f"/taxi/admin/driver/{driver_tg_id}")
        if response.status_code == 200:
            data = response.json()
            # Уведомляем водителя
            try:
                await callback.bot.send_message(
                    data["telegram_id"],
                    "❌ Вы были удалены из списка водителей CHVK City.",
                )
            except Exception:
                pass
            try:
                await callback.message.edit_text(callback.message.text + "\n\n❌ Водитель уволен")
            except Exception:
                pass
            await callback.answer("Водитель уволен", show_alert=False)
        else:
            await callback.answer("Ошибка при увольнении водителя", show_alert=True)
    except Exception as e:
        logger.exception(f"Error in fire_driver_callback: {e}")
        await callback.answer("❌ Ошибка при увольнении водителя", show_alert=True)

@router.callback_query(F.data.startswith("ignore_"))
async def ignore_order_callback(callback: CallbackQuery):
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer("Заказ скрыт")

@router.callback_query(F.data.startswith("at_place_"))
async def at_place_callback(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split("_")[-1])
    try:
        # Обновляем статус заказа на стороне бэкенда и получаем данные клиента
        response = await get_http_client().post(f"/taxi/order/{order_id}/at_place")
        if response.status_code == 200:
            order = response.json()
            client_chat_id = order["client_telegram_id"]
            driver_telegram_id = order.get("driver_telegram_id") or callback.from_user.id
            # Уведомляем клиента, с защитой от попытки писать боту
            try:
                me = await callback.bot.get_me()
                if client_chat_id == me.id:
                    logger.warning(
                        "Попытка отправить 'я на месте' самому боту (client_telegram_id=%s).",
                        client_chat_id,
                    )
                else:
                    text = "🚖 Водитель ожидает вас по адресу! Пожалуйста, выходите."
                    if client_chat_id == me.id:
                        logger.warning("Ошибка: попытка отправить сообщение боту")
                        return
                    logger.info("Sending at-place notification to passenger %s for order %s", client_chat_id, order_id)
                    p_state = _get_passenger_state(callback.bot, state.storage, client_chat_id)
                    pdata = await p_state.get_data()
                    active_id = pdata.get("active_message_id")
                    edited = False
                    if isinstance(active_id, int):
                        try:
                            await callback.bot.edit_message_text(
                                chat_id=client_chat_id,
                                message_id=active_id,
                                text=text,
                                reply_markup=keyboards.get_client_at_place_keyboard(order_id, driver_telegram_id),
                            )
                            edited = True
                            await p_state.update_data(
                                msg_to_delete=[active_id],
                                waiting_message_id=active_id,
                            )
                        except Exception:
                            pass
                    if not edited:
                        if isinstance(active_id, int):
                            try:
                                await callback.bot.delete_message(chat_id=client_chat_id, message_id=active_id)
                            except Exception:
                                pass
                        sent = await callback.bot.send_message(
                            client_chat_id,
                            text,
                            reply_markup=keyboards.get_client_at_place_keyboard(order_id, driver_telegram_id),
                        )
                        await p_state.update_data(
                            active_message_id=sent.message_id,
                            msg_to_delete=[sent.message_id],
                            waiting_message_id=sent.message_id,
                        )
            except Exception as e:
                logger.exception(f"Error sending 'at place' message to client {client_chat_id}: {e}")

            # Сообщение водителю
            await callback.answer("Вы отметили, что на месте ✅")
            try:
                await callback.message.edit_text(
                    f"🚕 Вы на месте по заказу #{order_id}.\n\n"
                    "Ожидаем клиента. Нажмите 'Начать поездку', когда он сядет в машину.",
                    reply_markup=keyboards.get_at_place_driver_keyboard(order_id, client_chat_id),
                )
            except Exception:
                pass
        else:
            await callback.answer("Ошибка при получении данных заказа", show_alert=True)
    except Exception as e:
        logger.exception(f"Error in at_place_callback: {e}")
        await callback.answer("❌ Ошибка при отправке уведомления", show_alert=True)


@router.callback_query(F.data.startswith("client_out_"))
async def client_out_callback(callback: CallbackQuery, state: FSMContext):
    """
    Клиент нажал '🏃 Выхожу!'.
    Уведомляем водителя и подтверждаем клиенту.
    """
    order_id = int(callback.data.split("_")[-1])
    try:
        response = await get_http_client().get(f"/taxi/order/{order_id}")
        if response.status_code == 200:
            order = response.json()
            driver_chat_id = order.get("driver_telegram_id")

            # Уведомление ТОЛЬКО персональному водителю
            if driver_chat_id is not None:
                logger.info(f"Sending 'out' notification to driver {driver_chat_id}")
                try:
                    me = await callback.bot.get_me()
                    if driver_chat_id == me.id:
                        logger.warning(
                            "Попытка отправить 'клиент выходит' самому боту (driver_telegram_id=%s).",
                            driver_chat_id,
                        )
                    else:
                        comment = order.get("comment") or "Нет"
                        sent = await callback.bot.send_message(
                            chat_id=driver_chat_id,
                            text=(
                                f"🔔 Клиент сообщил, что уже выходит по заказу #{order_id}!\n\n"
                                f"💬 Примечание: {comment}"
                            ),
                        )
                        # Сохраняем ID уведомления, чтобы потом его удалить при начале/завершении поездки.
                        # ВАЖНО: сохраняем его в FSM-контексте ИМЕННО водителя, а не клиента.
                        driver_key = StorageKey(
                            bot_id=callback.bot.id,
                            chat_id=driver_chat_id,
                            user_id=driver_chat_id,
                        )
                        driver_state = FSMContext(storage=state.storage, key=driver_key)
                        await driver_state.update_data(last_out_msg_id=sent.message_id)
                except Exception as e:
                    logger.exception(f"Error sending 'client out' message to driver {driver_chat_id}: {e}")
            else:
                logger.error(f"driver_telegram_id is None for order {order_id} in client_out_callback")

            # Обновляем сообщение клиента — только информационный текст, без кнопок
            try:
                mobj = await callback.message.edit_text(
                    "✅ Водитель уведомлен. Пожалуйста, выходите к машине. Приятного пути! 🚕",
                    reply_markup=None,
                )
                p_state = _get_passenger_state(callback.bot, state.storage, callback.from_user.id)
                pdata = await p_state.get_data()
                to_del = pdata.get("msg_to_delete", [])
                to_del.append(mobj.message_id if hasattr(mobj, "message_id") else callback.message.message_id)
                await p_state.update_data(msg_to_delete=to_del)
            except Exception:
                pass
        else:
            logger.error(
                "Ошибка получения заказа при client_out: %s %s",
                response.status_code,
                response.text,
            )
            await callback.answer("Не удалось уведомить водителя, попробуйте позже.", show_alert=True)
    except Exception as e:
        logger.exception(f"Error in client_out_callback: {e}")
        await callback.answer("Ошибка при обработке нажатия, попробуйте ещё раз.", show_alert=True)


@router.callback_query(F.data.startswith("client_call_"))
async def client_call_callback(callback: CallbackQuery):
    """
    Клиент нажал '📱 Позвонить'.
    Отправляем контакт водителя, чтобы смартфон сразу предложил звонок.
    """
    order_id = int(callback.data.split("_")[-1])
    try:
        response = await get_http_client().get(f"/taxi/order/{order_id}")
        if response.status_code == 200:
            order = response.json()
            driver_phone = order.get("driver_phone")
            if driver_phone:
                try:
                    await callback.bot.send_contact(
                        chat_id=callback.from_user.id,
                        phone_number=driver_phone,
                        first_name="Водитель",
                    )
                except Exception as e:
                    logger.exception(f"Error sending driver contact: {e}")
                    # fallback на текст, если send_contact не сработал
                    await callback.bot.send_message(
                        callback.from_user.id,
                        f"📞 Телефон водителя: {driver_phone}"
                    )
                await callback.answer()
            else:
                await callback.answer("Номер телефона водителя не указан.", show_alert=True)
        else:
            logger.error(
                "Ошибка получения заказа при client_call: %s %s",
                response.status_code,
                response.text,
            )
            await callback.answer("Не удалось получить данные для звонка.", show_alert=True)
    except Exception as e:
        logger.exception(f"Error in client_call_callback: {e}")
        await callback.answer("Ошибка при попытке получить номер водителя.", show_alert=True)


@router.callback_query(F.data.startswith("driver_call_"))
async def driver_call_callback(callback: CallbackQuery):
    """
    Водитель нажал '📞 Позвонить клиенту'.
    Отправляем контакт клиента водителю.
    """
    order_id = int(callback.data.split("_")[-1])
    try:
        response = await get_http_client().get(f"/taxi/order/{order_id}")
        if response.status_code == 200:
            order = response.json()
            client_phone = order.get("client_phone")
            if client_phone:
                try:
                    await callback.bot.send_contact(
                        chat_id=callback.from_user.id,
                        phone_number=client_phone,
                        first_name="Клиент",
                    )
                except Exception as e:
                    logger.exception(f"Error sending client contact: {e}")
                    await callback.bot.send_message(
                        callback.from_user.id,
                        f"📞 Телефон клиента: {client_phone}",
                    )
                await callback.answer()
            else:
                await callback.answer("Номер телефона клиента не указан.", show_alert=True)
        else:
            await callback.answer("Не удалось получить данные заказа.", show_alert=True)
    except Exception as e:
        logger.exception(f"Error in driver_call_callback: {e}")
        await callback.answer("Ошибка при попытке получить номер клиента.", show_alert=True)


@router.callback_query(F.data.startswith("driver_cancel_"))
async def driver_cancel_callback(callback: CallbackQuery):
    # Отмена поездки со стороны водителя
    order_id = int(callback.data.split("_")[-1])
    driver_telegram_id = callback.from_user.id

    try:
        response = await get_http_client().post(
            "/taxi/order/driver_cancel",
            json={
                "order_id": order_id,
                "driver_telegram_id": driver_telegram_id,
            },
        )
        if response.status_code == 200:
            data = response.json()
            client_chat_id = data.get("client_telegram_id")

            # Уведомляем клиента, что водитель отменил поездку
            if client_chat_id is not None:
                try:
                    me = await callback.bot.get_me()
                    if client_chat_id == me.id:
                        logger.warning(
                            "Попытка уведомить самого бота о отмене поездки (client_telegram_id=%s).",
                            client_chat_id,
                        )
                    else:
                        await callback.bot.send_message(
                            client_chat_id,
                            "❌ Водитель отменил поездку. Мы ищем для вас другого водителя.",
                        )
                except Exception as e:
                    logger.exception(f"Error sending 'driver cancelled' message to client {client_chat_id}: {e}")

            await callback.answer("Поездка отменена. Заказ снова доступен другим водителям.", show_alert=True)
            try:
                await callback.message.edit_text(
                    f"❌ Вы отменили заказ #{order_id}. Заказ возвращён в общий список.",
                )
            except Exception:
                pass
        else:
            # Попробуем показать detail из ответа
            detail_msg = None
            try:
                payload = response.json()
                if isinstance(payload, dict):
                    detail_msg = payload.get("detail")
            except Exception:
                detail_msg = None
            if not isinstance(detail_msg, str) or not detail_msg:
                detail_msg = "Не удалось отменить заказ. Возможно, он уже завершён или отменён."

            logger.error(
                "Ошибка driver_cancel_order: %s %s",
                response.status_code,
                response.text,
            )
            await callback.answer(detail_msg, show_alert=True)
    except Exception as e:
        logger.exception(f"Error in driver_cancel_callback: {e}")
        await callback.answer("❌ Ошибка при отмене поездки", show_alert=True)


@router.callback_query(F.data.startswith("start_trip_"))
async def start_trip_callback(callback: CallbackQuery, state: FSMContext):
    """
    Водитель нажал '🚀 Начать поездку'.
    Переводим заказ в статус 'in_progress', обновляем интерфейс водителя и уведомляем клиента.
    """
    order_id = int(callback.data.split("_")[-1])

    try:
        # Пытаемся удалить предыдущее уведомление "клиент выходит", если оно было
        try:
            data = await state.get_data()
            out_msg_id = data.get("last_out_msg_id")
            if isinstance(out_msg_id, int):
                try:
                    await callback.bot.delete_message(
                        chat_id=callback.from_user.id,
                        message_id=out_msg_id,
                    )
                    # очищаем ID, чтобы не пытаться удалить повторно
                    await state.update_data(last_out_msg_id=None)
                except Exception:
                    pass
        except Exception:
            pass

        response = await get_http_client().post(f"/taxi/order/{order_id}/start")
        if response.status_code == 200:
            data = response.json()
            client_chat_id = data.get("client_telegram_id")
            to_address = data.get("to_address", "Адрес не указан")

            try:
                await callback.message.edit_text(
                    "🚕 ПОЕЗДКА НАЧАТА.\n\n"
                    "Ожидайте указаний по маршруту и следуйте к точке назначения.",
                    reply_markup=keyboards.get_in_progress_driver_keyboard(order_id, to_address),
                )
            except Exception:
                pass
            await callback.answer("Поездка начата 🚕")

            # Уведомляем клиента: EDIT активного сообщения → «Поехали!» (State-based UI)
            if client_chat_id is not None:
                try:
                    me = await callback.bot.get_me()
                    if client_chat_id == me.id:
                        logger.warning("Ошибка: попытка отправить сообщение боту")
                        return
                    p_state = _get_passenger_state(callback.bot, state.storage, client_chat_id)
                    p_data = await p_state.get_data()
                    active_id = p_data.get("active_message_id")
                    edited = False
                    if isinstance(active_id, int):
                        try:
                            await callback.bot.edit_message_text(
                                chat_id=client_chat_id,
                                message_id=active_id,
                                text="🚀 Поехали! Приятного пути.",
                                reply_markup=None,
                            )
                            edited = True
                            await p_state.update_data(waiting_message_id=None)
                        except Exception:
                            pass
                    if not edited:
                        if isinstance(active_id, int):
                            try:
                                await callback.bot.delete_message(chat_id=client_chat_id, message_id=active_id)
                            except Exception:
                                pass
                        sent = await callback.bot.send_message(
                            client_chat_id,
                            "🚀 Поехали! Приятного пути.",
                        )
                        await p_state.update_data(
                            active_message_id=sent.message_id,
                            msg_to_delete=[sent.message_id],
                            waiting_message_id=None,
                        )
                    logger.info("Sending start-trip notification to passenger %s for order %s", client_chat_id, order_id)
                except Exception as e:
                    logger.exception(f"Error updating passenger messages in start_trip_callback: {e}")
        else:
            # detail из ответа
            detail_msg = None
            try:
                payload = response.json()
                if isinstance(payload, dict):
                    detail_msg = payload.get("detail")
            except Exception:
                detail_msg = None
            if not isinstance(detail_msg, str) or not detail_msg:
                detail_msg = "Не удалось начать поездку. Проверьте статус заказа."

            logger.error("Ошибка start_order: %s %s", response.status_code, response.text)
            await callback.answer(detail_msg, show_alert=True)
    except Exception as e:
        logger.exception(f"Error in start_trip_callback: {e}")
        await callback.answer("❌ Ошибка при начале поездки", show_alert=True)


@router.callback_query(F.data == "start_new_order")
async def start_new_order_callback(callback: CallbackQuery, state: FSMContext):
    """
    Клиент нажал '🚖 Заказать такси' после завершения заказа.
    Kill list: приветствие, старый «Заказать такси», «Дополнительные функции». Карточки сохраняем.
    """
    await callback.answer()
    chat_id = callback.message.chat.id
    data = await state.get_data()
    for mid in _get_technical_messages_kill_list(data):
        await _delete_or_clear_buttons_safe(callback.bot, chat_id, mid)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await state.clear()

    # Сразу запускаем новый заказ
    try:
        await _begin_order_flow(callback.message, state, callback.from_user.id)
    except Exception as e:
        logger.exception(f"Error sending new order prompt: {e}")
        await callback.answer("Произошла ошибка, попробуйте ещё раз.", show_alert=True)
        return

@router.callback_query(F.data.startswith("rate_"))
async def rate_trip_callback(callback: CallbackQuery):
    """
    Обработка оценки поездки.
    callback.data имеет вид 'rate_{оценка}_{order_id}'.
    """
    parts = callback.data.split("_")
    # parts[0] = 'rate', parts[1] = оценка, parts[2] = order_id (если нет подчёркиваний в середине)
    rating_str = parts[1] if len(parts) > 2 else "0"
    order_id = int(parts[-1])

    try:
        # Финальная карточка заказа: маршрут, цена, водитель, оценка, кнопка Поддержка
        order_resp = await get_http_client().get(f"/taxi/order/{order_id}")
        ord_data = order_resp.json() if order_resp.status_code == 200 else {}
        driver_name = ord_data.get("driver_name", "R S")
        car_model = ord_data.get("car_model", "Гранта")
        car_number = ord_data.get("car_number", "255")
        route_text = _format_route_from_values(
            ord_data.get("from_address"), ord_data.get("to_address")
        )
        price = ord_data.get("price")
        price_str = f"{price:.0f} руб." if isinstance(price, (int, float)) else "—"

        new_text = (
            "✅ Поездка завершена!\n\n"
            f"{route_text}\n\n"
            f"🚖 Ваш заказ выполнил: {driver_name}\n"
            f"🚗 Машина: {car_model}\n"
            f"🔢 Номер: {car_number}\n\n"
            f"💰 Стоимость: {price_str}\n\n"
            f"⭐ Ваша оценка: {rating_str} из 5\n"
            "🙏 Спасибо за отзыв!"
        )

        try:
            support_kb = keyboards.get_support_only_keyboard()
            await callback.message.edit_text(
                new_text,
                reply_markup=support_kb,
            )
        except Exception:
            pass
    except Exception as e:
        logger.exception(f"Error in rate_trip_callback: {e}")
    await callback.answer("Спасибо за оценку!")

    # Оценка анонимна: водитель НЕ получает уведомление о конкретной оценке.
    # Оценка отображается только клиенту в сообщении выше.

# Блокируем любой текст на этапах опций и подтверждения заказа.
# Исключение: комментарий принимается только в состоянии waiting_for_comment.
@router.message(OrderTaxi.waiting_for_options)
@router.message(OrderTaxi.waiting_for_confirmation)
async def any_text_in_order_process(message: Message):
    try:
        await message.delete()
    except Exception:
        pass

# Последний хендлер в файле — блокировщик: если бот в любом состоянии (ждёт адрес и т.д.), не трогаем сообщение

# Кнопки и команды, сообщения с которыми НЕ удаляем (админка/меню)
_ADMIN_SAFE_TEXTS = {
    "/start",
    "/driver",
    "🚕 Заказать такси",
    "🚖 Заказать такси",
    "🗂 Мои заказы",
    "📞 Поддержка",
    "💼 Кабинет водителя",
    "🚗 Стать водителем",
    "⚙️ Админка",
    "💎 УПРАВЛЕНИЕ",
    "👥 Водители в штате",
    "📩 Новые заявки",
    "❌ Удалить водителя (по ID)",
    "🔙 Назад",
    "👥 Список водителей",
    "✅ Одобрить новичков",
    "❌ Уволить водителя",
    "📊 Статистика заказов",
    "▶️ Выйти на смену",
    "⏸ Уйти со смены",
}

@router.message()
async def smart_delete_handler(message: Message, state: FSMContext):
    state_now = await state.get_state()
    if state_now is not None:
        return  # Если бот в любом состоянии (ждёт адрес, телефон, комментарий) — этот код НЕ должен работать!

    if message.text and message.text in _ADMIN_SAFE_TEXTS:
        return  # Кнопки меню и админки не удаляем

    try:
        await message.delete()
    except Exception:
        pass
