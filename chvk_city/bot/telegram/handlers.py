import asyncio
import datetime
import logging
import httpx
from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
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
    get_zone_by_address_geocoded,
    get_zone_price,
    get_ride_minutes,
    get_poi,
    geocode_full,
    geocode_suggest,
    get_driving_distance_km,
    INTERCITY_RATE_PER_KM,
    INTERCITY_NOTE,
    DEFAULT_ZONE_PRICE,
    DEFAULT_PRICE_NOTE,
)

logger = logging.getLogger(__name__)


def _is_owner(user_id: int) -> bool:
    """Проверка доступа владельца (OWNER_ID)."""
    return user_id == OWNER_ID
router = Router()

online_drivers: set[int] = set()
# Очередь водителей (FIFO по времени выхода на смену)
driver_queue: list[int] = []
# order_id -> driver_tg_id, которому сделано предложение
pending_offers: dict[int, int] = {}
# order_id -> asyncio.Task (таймер предложения)
offer_tasks: dict[int, asyncio.Task] = {}
# order_id -> {driver_msg, is_intercity}
pending_order_data: dict[int, dict] = {}

PENALTY_AMOUNT = 7.5
OFFER_TIMEOUT_CITY = 20
OFFER_TIMEOUT_INTERCITY = 40

# order_id -> asyncio.Task (уведомление водителей при наступлении времени предзаказа)
preorder_tasks: dict[int, asyncio.Task] = {}

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
    # Перехватываем /start: сбрасываем состояние и уходим в главное меню
    if message.text == "/start":
        await state.clear()
        await cmd_start(message, state)
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
        await message.answer("Сейчас я жду адрес, пожалуйста, напишите его текстом.")
        return
    raw = (message.text or "").strip()
    if len(raw) < 3:
        await message.answer("Пожалуйста, напишите адрес отправления текстом.")
        return

    try:
        await message.delete()
    except Exception:
        pass

    # Запрашиваем подсказки из Яндекс
    suggestions = await geocode_suggest(raw, n=4)

    if not suggestions:
        # Яндекс ничего не нашёл — fallback к старому geocode_full
        geo = await geocode_full(raw)
        await state.update_data(
            from_address=raw,
            from_zone=geo["zone"],
            from_coords=[geo["lon"], geo["lat"]] if geo["lon"] is not None else None,
        )
        data = await state.get_data()
        await _delete_messages(message.bot, message.chat.id, data.get("msg_to_delete", []))
        await state.update_data(msg_to_delete=[])
        await _prompt_for_to_address(message, state, message.from_user.id)
        return

    if len(suggestions) == 1 and get_poi(raw):
        # POI — единственный точный вариант, не требует выбора
        s = suggestions[0]
        data = await state.get_data()
        await _delete_messages(message.bot, message.chat.id, data.get("msg_to_delete", []))
        await state.update_data(
            from_address=s["display"], from_zone=s["zone"],
            from_coords=[s["lon"], s["lat"]], msg_to_delete=[],
        )
        print(f"DEBUG ORDER: POI from='{s['display']}', zone={s['zone']!r}", flush=True)
        await _prompt_for_to_address(message, state, message.from_user.id)
        return

    # Показываем кнопки-подсказки
    data = await state.get_data()
    await _delete_messages(message.bot, message.chat.id, data.get("msg_to_delete", []))
    await state.update_data(pending_suggestions=suggestions, pending_suggest_type="from")
    sent = await message.answer(
        "📍 Уточните адрес отправления:",
        reply_markup=keyboards.get_address_suggestions_keyboard(suggestions, "from"),
    )
    await state.update_data(msg_to_delete=[sent.message_id])


@router.message(OrderTaxi.waiting_for_to_address, F.text)
async def process_to_address(message: Message, state: FSMContext):
    if message.text and message.text.startswith('/'):
        return
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
    raw = (message.text or "").strip()
    if len(raw) < 3:
        await message.answer("Пожалуйста, напишите адрес назначения текстом.")
        return

    try:
        await message.delete()
    except Exception:
        pass

    # Запрашиваем подсказки из Яндекс
    suggestions = await geocode_suggest(raw, n=4)

    if not suggestions:
        # Яндекс ничего не нашёл — fallback к geocode_full
        geo = await geocode_full(raw)
        to_zone = geo["zone"]
        to_coord = [geo["lon"], geo["lat"]] if geo["lon"] is not None else None
        to_zones = list(data.get("to_zones") or [])
        to_zones.append(to_zone)
        to_coords_list = list(data.get("to_coords_list") or [])
        to_coords_list.append(to_coord)
        zone_updates: dict = {"to_zones": to_zones, "to_coords_list": to_coords_list}
        if not data.get("destination_addresses"):
            zone_updates["to_zone"] = to_zone
        await state.update_data(**zone_updates)
        prev_msg_ids = data.get("msg_to_delete", [])
        edit_id = data.get("route_message_id") or (prev_msg_ids[-1] if prev_msg_ids else None)
        await _save_destination_and_show_options(message, state, raw, message.from_user.id, edit_message_id=edit_id)
        return

    if len(suggestions) == 1 and get_poi(raw):
        # POI — единственный точный вариант, не требует выбора
        s = suggestions[0]
        to_zone = s["zone"]
        to_coord = [s["lon"], s["lat"]]
        to_zones = list(data.get("to_zones") or [])
        to_zones.append(to_zone)
        to_coords_list = list(data.get("to_coords_list") or [])
        to_coords_list.append(to_coord)
        zone_updates = {"to_zones": to_zones, "to_coords_list": to_coords_list}
        if not data.get("destination_addresses"):
            zone_updates["to_zone"] = to_zone
        await state.update_data(**zone_updates)
        prev_msg_ids = data.get("msg_to_delete", [])
        edit_id = data.get("route_message_id") or (prev_msg_ids[-1] if prev_msg_ids else None)
        print(f"DEBUG ORDER: POI to='{s['display']}', zone={s['zone']!r}", flush=True)
        await _save_destination_and_show_options(message, state, s["display"], message.from_user.id, edit_message_id=edit_id)
        return

    # Показываем кнопки-подсказки
    await _delete_messages(message.bot, message.chat.id, data.get("msg_to_delete", []))
    await state.update_data(pending_suggestions=suggestions, pending_suggest_type="to")
    sent = await message.answer(
        "🏁 Уточните адрес назначения:",
        reply_markup=keyboards.get_address_suggestions_keyboard(suggestions, "to"),
    )
    await state.update_data(msg_to_delete=[sent.message_id])


@router.callback_query(F.data.startswith("saddr:"))
async def suggest_addr_callback(callback: CallbackQuery, state: FSMContext):
    """Пользователь выбрал конкретный вариант адреса из подсказок."""
    parts = callback.data.split(":", 2)  # saddr:{from|to}:{idx}
    addr_type = parts[1]
    idx = int(parts[2])

    data = await state.get_data()
    suggestions = data.get("pending_suggestions") or []
    if idx >= len(suggestions):
        await callback.answer("Ошибка: вариант не найден", show_alert=True)
        return

    s = suggestions[idx]
    await callback.answer()

    try:
        await callback.message.delete()
    except Exception:
        pass

    if addr_type == "from":
        await state.update_data(
            from_address=s["display"],
            from_zone=s["zone"],
            from_coords=[s["lon"], s["lat"]] if s["lon"] is not None else None,
            pending_suggestions=[],
            msg_to_delete=[],
        )
        print(
            f"DEBUG ORDER: suggest→from='{s['display']}', zone={s['zone']!r}, "
            f"coords=({s['lon']},{s['lat']})",
            flush=True,
        )
        await _prompt_for_to_address(callback.message, state, callback.from_user.id)

    elif addr_type == "to":
        to_zone = s["zone"]
        to_coord = [s["lon"], s["lat"]] if s["lon"] is not None else None
        to_zones = list(data.get("to_zones") or [])
        to_zones.append(to_zone)
        to_coords_list = list(data.get("to_coords_list") or [])
        to_coords_list.append(to_coord)
        zone_updates: dict = {"to_zones": to_zones, "to_coords_list": to_coords_list, "pending_suggestions": []}
        if not data.get("destination_addresses"):
            zone_updates["to_zone"] = to_zone
        await state.update_data(**zone_updates)
        print(
            f"DEBUG ORDER: suggest→to='{s['display']}', zone={s['zone']!r}, "
            f"coords=({s['lon']},{s['lat']})",
            flush=True,
        )
        await _save_destination_and_show_options(
            callback.message, state, s["display"], callback.from_user.id, edit_message_id=None,
        )


@router.callback_query(F.data.startswith("saddr_reenter:"))
async def suggest_reenter_callback(callback: CallbackQuery, state: FSMContext):
    """Пользователь хочет ввести другой адрес вручную."""
    addr_type = callback.data.split(":", 1)[1]
    await callback.answer()
    header = "📍 Откуда вас забрать?" if addr_type == "from" else "🏁 Куда едем?"
    try:
        await callback.message.edit_text(
            f"{header}\n\nНапишите адрес (улица, номер дома):",
            reply_markup=None,
        )
    except Exception:
        pass


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
        text="✍️ Ввести адрес вручную",
        callback_data=manual_callback,
    )
    if step == "to":
        builder.button(text="⬅️ Назад", callback_data="back_to_from")
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
            # Удаляем все отслеживаемые сообщения, включая текущее
            all_to_delete = list({edit_message_id, *msg_list})
            await _delete_messages(target_message.bot, target_message.chat.id, all_to_delete)
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


def _zone_for_address(address: str, stored_zone: str | None = None) -> str | None:
    """Определяет зону для адреса: stored_zone → POI → ключевые слова."""
    if stored_zone:
        return stored_zone
    poi = get_poi(address)
    if poi:
        return poi["zone"]
    return get_zone_by_address(address)


_SAMARA_TZ = datetime.timezone(datetime.timedelta(hours=4))

_RU_MONTHS = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля",
    5: "мая", 6: "июня", 7: "июля", 8: "августа",
    9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}


def _format_preorder_display(dt: datetime.datetime) -> str:
    """Форматирует дату/время предзаказа: «21 марта, 04:15»."""
    return f"{dt.day} {_RU_MONTHS[dt.month]}, {dt.strftime('%H:%M')}"


def _night_surcharge_per_segment() -> float:
    """
    Возвращает ночную надбавку на один сегмент (по времени Самары, UTC+4):
      22:00–01:00 → +50 руб.
      01:00–05:00 → +100 руб.
      остальное   → 0 руб.
    """
    now = datetime.datetime.now(_SAMARA_TZ)
    h = now.hour
    if 22 <= h or h < 1:
        return 50.0
    if 1 <= h < 5:
        return 100.0
    return 0.0


async def _estimate_order_price(data: dict) -> tuple[float, str | None, bool]:
    """
    Рассчитывает стоимость маршрута по сегментам: A→B + B→C + ...
    ПРИОРИТЕТ: сначала считаем дистанцию. Если > 15 км — ВСЕГДА загородный тариф (28₽/км).
    """
    from_address = data.get("from_address") or ""
    destination_addresses = data.get("destination_addresses") or []
    all_addresses = [from_address] + destination_addresses

    # Зоны из геокодера (используются только для коротких маршрутов < 15 км)
    to_zones_stored = data.get("to_zones") or []
    stored_zones: dict[int, str | None] = {0: data.get("from_zone")}
    for i, z in enumerate(to_zones_stored):
        stored_zones[i + 1] = z

    # Координаты из геокодера
    to_coords_stored = data.get("to_coords_list") or []
    stored_coords: dict[int, list | None] = {0: data.get("from_coords")}
    for i, c in enumerate(to_coords_stored):
        stored_coords[i + 1] = c

    night_surcharge = _night_surcharge_per_segment()
    total_legs = 0.0
    any_unrecognized = False
    any_intercity = False

    for i in range(len(all_addresses) - 1):
        addr_a = all_addresses[i]
        addr_b = all_addresses[i + 1]
        coords_a = stored_coords.get(i)
        coords_b = stored_coords.get(i + 1)

        # ШАГ 1: запросить дистанцию, если есть координаты
        dist_km: float | None = None
        if coords_a and coords_b:
            dist_km = await get_driving_distance_km(
                coords_a[0], coords_a[1], coords_b[0], coords_b[1]
            )

        dist_m = (dist_km * 1000) if dist_km is not None else None

        # ШАГ 2: если дистанция > 15 км — загородный тариф, зоны игнорируем
        if dist_m is not None and dist_m > 15000:
            leg_price = round(dist_km * INTERCITY_RATE_PER_KM)
            any_intercity = True
            print(
                f"DEBUG PRICE: dist={dist_m:.0f}m ({dist_km:.1f} км), "
                f"intercity=True, leg={leg_price}₽, final_price={leg_price + night_surcharge:.0f}₽",
                flush=True,
            )
        else:
            # ШАГ 3: короткий маршрут — используем таблицу зон
            zone_a = _zone_for_address(addr_a, stored_zones.get(i))
            zone_b = _zone_for_address(addr_b, stored_zones.get(i + 1))

            if zone_a is None or zone_b is None:
                # Нет зоны, но дистанция маленькая или неизвестна
                if dist_km is not None:
                    leg_price = round(dist_km * INTERCITY_RATE_PER_KM)
                    any_intercity = True
                else:
                    leg_price = DEFAULT_ZONE_PRICE
                    any_unrecognized = True
            else:
                leg_price, recognized = get_zone_price(zone_a, zone_b)
                if not recognized:
                    any_unrecognized = True

            print(
                f"DEBUG PRICE: dist={dist_m or 0:.0f}m, "
                f"zones=({zone_a}→{zone_b}), leg={leg_price}₽, "
                f"final_price={leg_price + night_surcharge:.0f}₽",
                flush=True,
            )

        total_legs += leg_price + night_surcharge

    child_seat_price = 48.0 if data.get("has_child_seat") else 0.0
    pet_price = 48.0 if data.get("has_pet") else 0.0
    total = total_legs + child_seat_price + pet_price

    if any_intercity:
        price_note = INTERCITY_NOTE
    elif any_unrecognized:
        price_note = DEFAULT_PRICE_NOTE
    else:
        price_note = None

    return total, price_note, any_intercity


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
    price_text = f"<b>{price:.0f} руб.</b>" if isinstance(price, (int, float)) else "—"
    if price_note:
        price_text += f"\n<i>{price_note}</i>"

    preorder_str = data.get("preorder_time_str")
    preorder_line = f"\n\n🕒 <b>Время подачи: {preorder_str}</b>" if preorder_str else ""

    return (
        "✅ <b>Итог заказа</b>\n\n"
        f"<b>{_format_route_vertical(from_address, destination_addresses)}</b>\n\n"
        f"🛠 Опции:\n{options_text}\n\n"
        f"💰 Стоимость: {price_text}"
        f"{preorder_line}"
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
        from_zone=None,
        from_coords=None,
        to_zone=None,
        to_zones=[],
        to_coords_list=[],
        has_child_seat=False,
        has_pet=False,
        order_comment=None,
        calculated_price=None,
        price_note=None,
        is_processing=False,
        order_id=None,
        preorder_scheduled_at=None,
        preorder_time_str=None,
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
    # Удаляем команду пользователя /start
    try:
        await message.delete()
    except Exception:
        pass

    # Собираем все ID старых сообщений для удаления до очистки стейта
    data = await state.get_data()
    _ids_to_purge: list[int] = []
    for _key in ("last_bot_msg_id", "last_menu_msg_id", "notification_id", "last_new_order_prompt_id"):
        _mid = data.get(_key)
        if isinstance(_mid, int) and _mid not in _ids_to_purge:
            _ids_to_purge.append(_mid)
    for _mid in (data.get("start_message_ids") or []):
        if isinstance(_mid, int) and _mid not in _ids_to_purge:
            _ids_to_purge.append(_mid)
    for _mid in (data.get("messages_to_delete") or []):
        if isinstance(_mid, int) and _mid not in _ids_to_purge:
            _ids_to_purge.append(_mid)
    for _mid in (data.get("msg_to_delete") or []):
        if isinstance(_mid, int) and _mid not in _ids_to_purge:
            _ids_to_purge.append(_mid)
    await state.clear()
    for _mid in _ids_to_purge:
        try:
            await message.bot.delete_message(chat_id=message.chat.id, message_id=_mid)
        except Exception:
            pass

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

    try:
        welcome = await message.answer(
            "Привет! Я помогу вам заказать такси. Нажмите на кнопку ниже, чтобы начать.",
            reply_markup=keyboards.get_start_order_inline_keyboard(),
        )
        await state.update_data(
            last_bot_msg_id=welcome.message_id,
            last_menu_msg_id=welcome.message_id,
            start_message_ids=[welcome.message_id],
            messages_to_delete=[welcome.message_id],
        )
    except Exception as e:
        logger.error(f"Failed to send welcome to {user_id}: {e}", exc_info=True)
        print(f"[START] send welcome error: {e}", flush=True)

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

async def add_to_messages_to_delete(state: FSMContext, message_id: int):
    """Добавляет ID сервисного сообщения в messages_to_delete."""
    if not isinstance(message_id, int):
        return
    data = await state.get_data()
    lst = list(data.get("messages_to_delete") or [])
    if message_id not in lst:
        lst.append(message_id)
    await state.update_data(messages_to_delete=lst)


async def perform_cleanup(bot, chat_id: int, state: FSMContext):
    """Удаляет все сообщения из msg_to_cleanup и очищает список."""
    data = await state.get_data()
    ids = list(data.get("msg_to_cleanup") or [])
    for mid in ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            pass
    await state.update_data(msg_to_cleanup=[])


async def delete_messages_and_clear(bot, chat_id: int, state: FSMContext):
    """Удаляет все сообщения из messages_to_delete и очищает список. Вызывать ДО отправки нового сообщения."""
    data = await state.get_data()
    msg_ids = data.get("messages_to_delete", [])
    for msg_id in msg_ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass
    await state.update_data(messages_to_delete=[])


def _get_technical_messages_kill_list(data: dict) -> list[int]:
    """
    Kill list: временные уведомления и приветствия для удаления при новом заказе.
    НЕ включает карточки поездок (основная карточка, финальные плашки).
    """
    ids: list[int] = []
    for key in ("last_menu_msg_id", "last_new_order_prompt_id", "notification_id"):
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


async def _send_single_window(
    state: FSMContext,
    target: Message,
    text: str,
    reply_markup=None,
    parse_mode: str | None = None,
) -> Message:
    """
    Единое окно: перед отправкой нового сообщения удаляет предыдущее
    сообщение бота (ID хранится в FSMContext под ключом 'last_bot_msg_id').
    """
    data = await state.get_data()
    last_id = data.get("last_bot_msg_id")
    if isinstance(last_id, int):
        try:
            await target.bot.delete_message(chat_id=target.chat.id, message_id=last_id)
        except Exception:
            pass
    sent = await target.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
    await state.update_data(last_bot_msg_id=sent.message_id)
    return sent


@router.message(F.text == "🚖 Заказать такси")
@router.message(F.text == "🚕 Заказать такси")
async def taxi_order_start(message: Message, state: FSMContext):
    await message.delete()
    data = await state.get_data()
    messages_to_delete = data.get("messages_to_delete", [])

    # Удаляем сообщение пользователя (нажатие текстовой кнопки)
    try:
        await message.delete()
    except Exception:
        pass

    # Удаляем приветственное сообщение бота
    last_bot_msg_id = data.get("last_bot_msg_id")
    if isinstance(last_bot_msg_id, int):
        try:
            await message.bot.delete_message(chat_id=message.chat.id, message_id=last_bot_msg_id)
        except Exception:
            pass

    for mid in _get_technical_messages_kill_list(data):
        await _delete_or_clear_buttons_safe(message.bot, message.chat.id, mid)
    await state.clear()
    await state.update_data(messages_to_delete=messages_to_delete)

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
    messages_to_delete = data.get("messages_to_delete", [])
    for mid in _get_technical_messages_kill_list(data):
        await _delete_or_clear_buttons_safe(callback.bot, chat_id, mid)
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass
    await state.clear()
    await state.update_data(messages_to_delete=messages_to_delete)
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
async def support_handler(message: Message, state: FSMContext):
    """Кнопка «Поддержка» — краткая информация и меню."""
    await _send_single_window(
        state, message,
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
            await _send_single_window(
                state, message,
                "💼 Кабинет водителя\n\n"
                "Здесь вы можете выйти на смену или приостановить приём заказов.",
                reply_markup=keyboards.get_driver_menu(),
            )
            return
        await _send_single_window(
            state, message,
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
                await _send_single_window(
                    state, message,
                    "Вы уже одобренный водитель. Откройте кабинет.",
                    reply_markup=await _get_menu_for_user(telegram_id),
                )
                return
            await _send_single_window(
                state, message,
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
async def driver_cabinet_handler(message: Message, state: FSMContext):
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
        await _send_single_window(
            state, message,
            "❌ Временно недоступно. Попробуйте позже.",
            reply_markup=await _get_menu_for_user(telegram_id),
        )
        return

    if not driver:
        await _send_single_window(
            state, message,
            "❌ Не удалось получить данные водителя.",
            reply_markup=await _get_menu_for_user(telegram_id),
        )
        return

    if not driver.is_approved:
        await _send_single_window(
            state, message,
            "🕓 Ваша заявка на рассмотрении. Дождитесь одобрения.",
            reply_markup=await _get_menu_for_user(telegram_id),
        )
        return

    await _send_single_window(
        state, message,
        "💼 Кабинет водителя\n\n"
        "Здесь вы можете выйти на смену или приостановить приём заказов.",
        reply_markup=keyboards.get_driver_menu(),
    )


@router.message(F.text == "⚙️ Админка")
async def admin_panel_handler(message: Message, state: FSMContext):
    """
    Вход в админ-панель. Доступен только для владельца (OWNER_ID).
    """
    if not _is_owner(message.from_user.id):
        await _send_single_window(
            state, message,
            "⚠️ У вас нет доступа к админ-панели.",
            reply_markup=await _get_menu_for_user(message.from_user.id),
        )
        return

    await _send_single_window(
        state, message,
        "⚙️ Админ-панель. Выберите действие:",
        reply_markup=keyboards.get_admin_menu(),
    )


@router.message(F.text == "💎 УПРАВЛЕНИЕ")
async def owner_panel_handler(message: Message, state: FSMContext):
    """
    Панель владельца (OWNER_ID): отдельное меню управления.
    """
    if not _is_owner(message.from_user.id):
        await _send_single_window(
            state, message,
            "⚠️ У вас нет доступа к панели владельца.",
            reply_markup=await _get_menu_for_user(message.from_user.id),
        )
        return

    await _send_single_window(
        state, message,
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

    data = await state.get_data()
    _prev_bot_msg_id = data.get("last_bot_msg_id")
    await state.clear()
    if isinstance(_prev_bot_msg_id, int):
        try:
            await message.bot.delete_message(chat_id=message.chat.id, message_id=_prev_bot_msg_id)
        except Exception:
            pass
    sent = await message.answer(
        "Главное меню:",
        reply_markup=await _get_menu_for_user(message.from_user.id),
    )
    await state.update_data(last_bot_msg_id=sent.message_id)


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
    await _send_single_window(
        state, message,
        "<b>Выберите вашу текущую локацию (стоянку):</b>",
        reply_markup=keyboards.get_driver_districts_keyboard(),
        parse_mode="HTML",
    )


@router.message(F.text == "⏸ Уйти со смены")
async def driver_go_offline(message: Message, state: FSMContext):
    """Водитель уходит со смены: убираем его из локального списка онлайн-водителей."""
    drv_id = message.from_user.id
    online_drivers.discard(drv_id)
    if drv_id in driver_queue:
        driver_queue.remove(drv_id)
    await _send_single_window(
        state, message,
        "⏸ Вы ушли со смены. Новые заказы больше не будут приходить в этот чат.",
        reply_markup=keyboards.get_driver_menu(),
    )


@router.message(F.text == "💰 Мой баланс")
async def driver_balance_handler(message: Message, state: FSMContext):
    """Показывает текущий баланс водителя."""
    telegram_id = message.from_user.id
    try:
        resp = await get_http_client().get(f"/taxi/driver/{telegram_id}/balance")
        if resp.status_code == 200:
            balance = resp.json().get("balance", 0.0)
            sign = "+" if balance >= 0 else ""
            await message.answer(
                f"💰 Ваш баланс: <b>{sign}{balance:.2f} руб.</b>",
                parse_mode="HTML",
                reply_markup=keyboards.get_driver_menu(),
            )
        else:
            await message.answer("❌ Не удалось получить баланс.", reply_markup=keyboards.get_driver_menu())
    except Exception as e:
        logger.error(f"Failed to get balance for driver {telegram_id}: {e}")
        await message.answer("❌ Ошибка при получении баланса.", reply_markup=keyboards.get_driver_menu())


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

    # Проверка баланса: отрицательный баланс = нельзя выйти на смену
    try:
        bal_resp = await get_http_client().get(f"/taxi/driver/{message.from_user.id}/balance")
        if bal_resp.status_code == 200:
            balance = bal_resp.json().get("balance", 0.0)
            if balance < 0:
                await state.clear()
                await message.answer(
                    f"⛔ Вы не можете выйти на смену.\n"
                    f"Ваш баланс отрицательный: <b>{balance:.2f} руб.</b>\n\n"
                    "Пополните баланс у администратора.",
                    parse_mode="HTML",
                    reply_markup=keyboards.get_driver_menu(),
                )
                return
    except Exception as e:
        logger.error(f"Failed to check balance for driver {message.from_user.id}: {e}")

    online_drivers.add(message.from_user.id)
    drv_id = message.from_user.id
    if drv_id not in driver_queue:
        driver_queue.append(drv_id)
    await state.clear()
    await message.answer(
        f"✅ Вы встали в очередь в районе {district}. Ожидайте заказов.",
        reply_markup=keyboards.get_driver_menu(),
    )


@router.callback_query(F.data == "back_to_menu")
async def back_to_menu_callback(callback: CallbackQuery, state: FSMContext):
    """Возврат в главное меню из списка заказов или других экранов."""
    data = await state.get_data()
    _prev_bot_msg_id = data.get("last_bot_msg_id")
    await state.clear()
    await callback.answer()
    if isinstance(_prev_bot_msg_id, int):
        try:
            await callback.bot.delete_message(chat_id=callback.message.chat.id, message_id=_prev_bot_msg_id)
        except Exception:
            pass
    sent = await callback.message.answer(
        "Главное меню. Выберите действие:",
        reply_markup=await _get_menu_for_user(callback.from_user.id),
    )
    await state.update_data(last_bot_msg_id=sent.message_id)


@router.callback_query(F.data == "manual_from", OrderTaxi.waiting_for_from_address)
async def manual_from_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        await callback.message.edit_text(
            "📍 Откуда вас забрать?\n\nНапишите адрес (улица, номер дома):",
            reply_markup=None,
        )
        msg_id = callback.message.message_id
    except Exception:
        sent = await callback.message.answer("📍 Откуда вас забрать?\n\nНапишите адрес (улица, номер дома):")
        msg_id = sent.message_id
    await state.update_data(msg_to_delete=[msg_id])


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
        text = "🏁 Куда едем?\n\nНапишите адрес (улица, номер дома):"
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
    geo = await geocode_full(from_address)
    from_zone = geo["zone"]
    from_coords = [geo["lon"], geo["lat"]] if geo["lon"] is not None else None
    msg_list = data.get("msg_to_delete", [])
    msg_list.append(callback.message.message_id)
    await state.update_data(
        msg_to_delete=msg_list,
        from_address=from_address,
        from_zone=from_zone,
        from_coords=from_coords,
    )
    print(f"DEBUG ORDER: selected recent from_address='{from_address}', from_zone={from_zone!r} for user {callback.from_user.id}", flush=True)
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
    geo = await geocode_full(to_address)
    to_zone = geo["zone"]
    to_coord = [geo["lon"], geo["lat"]] if geo["lon"] is not None else None
    to_zones = list(data.get("to_zones") or [])
    to_zones.append(to_zone)
    to_coords_list = list(data.get("to_coords_list") or [])
    to_coords_list.append(to_coord)
    zone_updates: dict = {"to_zones": to_zones, "to_coords_list": to_coords_list}
    if not data.get("destination_addresses"):
        zone_updates["to_zone"] = to_zone
    await state.update_data(**zone_updates)
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


@router.callback_query(F.data == "back_to_from", OrderTaxi.waiting_for_to_address)
async def back_to_from_callback(callback: CallbackQuery, state: FSMContext):
    """Шаг 'Куда' → назад → шаг 'Откуда'. Сбрасывает from_address и всё что после."""
    await callback.answer()
    recent = await _get_recent_addresses(callback.from_user.id, "from")
    try:
        await callback.message.edit_text(
            "📍 Откуда вас забрать?\nВыберите из списка ниже или введите новый:",
            reply_markup=_build_recent_addresses_keyboard(recent, "from"),
        )
        msg_id = callback.message.message_id
    except Exception:
        data = await state.get_data()
        await _delete_messages(callback.bot, callback.message.chat.id, data.get("msg_to_delete", []))
        sent = await callback.message.answer(
            "📍 Откуда вас забрать?\nВыберите из списка ниже или введите новый:",
            reply_markup=_build_recent_addresses_keyboard(recent, "from"),
        )
        msg_id = sent.message_id
    await state.update_data(
        from_address=None,
        to_address=None,
        destination_addresses=[],
        recent_from_addresses=recent,
        route_message_id=None,
        msg_to_delete=[msg_id],
        from_zone=None,
        from_coords=None,
        to_zone=None,
        to_zones=[],
        to_coords_list=[],
    )
    await state.set_state(OrderTaxi.waiting_for_from_address)


@router.callback_query(F.data == "back_to_to_address", OrderTaxi.waiting_for_options)
async def back_to_to_address_callback(callback: CallbackQuery, state: FSMContext):
    """Шаг 'Опции' → назад → шаг 'Куда'. Сбрасывает to_address, сохраняет from_address."""
    await callback.answer()
    data = await state.get_data()
    from_address = data.get("from_address") or "—"
    recent = await _get_recent_addresses(callback.from_user.id, "to")
    text = f"📍 Откуда: {from_address}\n\n🏁 Куда едем?\nВыберите из списка ниже или введите новый:"
    try:
        await callback.message.edit_text(
            text,
            reply_markup=_build_recent_addresses_keyboard(recent, "to"),
        )
        msg_id = callback.message.message_id
    except Exception:
        await _delete_messages(callback.bot, callback.message.chat.id, data.get("msg_to_delete", []))
        sent = await callback.message.answer(text, reply_markup=_build_recent_addresses_keyboard(recent, "to"))
        msg_id = sent.message_id
    await state.update_data(
        to_address=None,
        destination_addresses=[],
        recent_to_addresses=recent,
        route_message_id=msg_id,
        msg_to_delete=[msg_id],
        to_zone=None,
        to_zones=[],
        to_coords_list=[],
    )
    await state.set_state(OrderTaxi.waiting_for_to_address)


@router.callback_query(F.data == "back_to_options", OrderTaxi.waiting_for_confirmation)
async def back_to_options_callback(callback: CallbackQuery, state: FSMContext):
    """Шаг 'Подтверждение' → назад → шаг 'Опции'. Данные не сбрасываются."""
    await callback.answer()
    data = await state.get_data()
    from_address = data.get("from_address") or "—"
    destination_addresses = data.get("destination_addresses") or []
    has_child_seat = bool(data.get("has_child_seat"))
    has_pet = bool(data.get("has_pet"))
    order_comment = data.get("order_comment")
    try:
        await callback.message.edit_text(
            _build_order_options_text(from_address, destination_addresses, order_comment),
            reply_markup=keyboards.get_order_options_keyboard(
                has_child_seat=has_child_seat,
                has_pet=has_pet,
            ),
        )
        msg_id = callback.message.message_id
    except Exception:
        await _delete_messages(callback.bot, callback.message.chat.id, data.get("msg_to_delete", []))
        sent = await callback.message.answer(
            _build_order_options_text(from_address, destination_addresses, order_comment),
            reply_markup=keyboards.get_order_options_keyboard(has_child_seat=has_child_seat, has_pet=has_pet),
        )
        msg_id = sent.message_id
    await state.update_data(msg_to_delete=[msg_id])
    await state.set_state(OrderTaxi.waiting_for_options)


@router.callback_query(F.data == "preorder_order", OrderTaxi.waiting_for_confirmation)
async def preorder_order_callback(callback: CallbackQuery, state: FSMContext):
    """Показывает клавиатуру выбора времени предзаказа."""
    await callback.answer()
    data = await state.get_data()
    try:
        await callback.message.edit_text(
            _build_final_summary_text(data) + "\n\n🕒 <b>Выберите время заказа:</b>",
            reply_markup=keyboards.get_preorder_time_keyboard(),
            parse_mode="HTML",
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("preorder_time:"), OrderTaxi.waiting_for_confirmation)
async def preorder_time_callback(callback: CallbackQuery, state: FSMContext):
    """Пользователь выбрал время предзаказа."""
    minutes = int(callback.data.split(":")[1])
    now = datetime.datetime.now(_SAMARA_TZ)
    scheduled_at = now + datetime.timedelta(minutes=minutes)
    scheduled_at_iso = scheduled_at.isoformat()
    time_str = _format_preorder_display(scheduled_at)

    await state.update_data(preorder_scheduled_at=scheduled_at_iso, preorder_time_str=time_str)
    await callback.answer(f"🕐 Предзаказ на {time_str}")

    data = await state.get_data()
    summary_data = {**data}
    try:
        await callback.message.edit_text(
            _build_final_summary_text(summary_data),
            reply_markup=keyboards.get_order_confirmation_keyboard(preorder_time_str=time_str),
            parse_mode="HTML",
        )
    except Exception:
        pass


@router.callback_query(F.data == "preorder_custom", OrderTaxi.waiting_for_confirmation)
async def preorder_custom_callback(callback: CallbackQuery, state: FSMContext):
    """Показывает выбор даты (Сегодня/Завтра/Послезавтра)."""
    await callback.answer()
    data = await state.get_data()
    try:
        await callback.message.edit_text(
            _build_final_summary_text(data) + "\n\n📅 <b>Выберите день предзаказа:</b>",
            reply_markup=keyboards.get_preorder_date_keyboard(),
            parse_mode="HTML",
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("preorder_pick_date:"), OrderTaxi.waiting_for_confirmation)
async def preorder_pick_date_callback(callback: CallbackQuery, state: FSMContext):
    """Пользователь выбрал дату — показываем сетку часов."""
    date_offset = int(callback.data.split(":")[1])
    await callback.answer()
    data = await state.get_data()
    try:
        await callback.message.edit_text(
            _build_final_summary_text(data) + "\n\n🕒 <b>Выберите часы:</b>",
            reply_markup=keyboards.get_preorder_hour_keyboard(date_offset),
            parse_mode="HTML",
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("preorder_pick_hour:"), OrderTaxi.waiting_for_confirmation)
async def preorder_pick_hour_callback(callback: CallbackQuery, state: FSMContext):
    """Пользователь выбрал час — показываем сетку минут."""
    _, date_offset_s, hour_s = callback.data.split(":")
    date_offset, hour = int(date_offset_s), int(hour_s)
    await callback.answer()
    data = await state.get_data()
    try:
        await callback.message.edit_text(
            _build_final_summary_text(data) + "\n\n🕒 <b>Выберите минуты:</b>",
            reply_markup=keyboards.get_preorder_minute_keyboard(date_offset, hour),
            parse_mode="HTML",
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("preorder_pick_min:"), OrderTaxi.waiting_for_confirmation)
async def preorder_pick_min_callback(callback: CallbackQuery, state: FSMContext):
    """Пользователь выбрал минуты — валидируем и сохраняем время предзаказа."""
    _, date_offset_s, hour_s, minute_s = callback.data.split(":")
    date_offset, hour, minute = int(date_offset_s), int(hour_s), int(minute_s)

    now = datetime.datetime.now(_SAMARA_TZ)
    target_date = now.date() + datetime.timedelta(days=date_offset)
    scheduled_at = datetime.datetime(
        target_date.year, target_date.month, target_date.day,
        hour, minute,
        tzinfo=_SAMARA_TZ,
    )

    if scheduled_at <= now:
        await callback.answer(
            "⛔ Это время уже прошло. Выберите время в будущем.",
            show_alert=True,
        )
        return

    scheduled_at_iso = scheduled_at.isoformat()
    time_str = _format_preorder_display(scheduled_at)

    await state.update_data(preorder_scheduled_at=scheduled_at_iso, preorder_time_str=time_str)
    await callback.answer(f"🕐 Предзаказ на {time_str}")

    data = await state.get_data()
    try:
        await callback.message.edit_text(
            _build_final_summary_text(data),
            reply_markup=keyboards.get_order_confirmation_keyboard(preorder_time_str=time_str),
            parse_mode="HTML",
        )
    except Exception:
        pass


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
        await callback.message.edit_text(
            "✍️ Напишите комментарий для водителя.\nНапример: Подъезд 2.",
            reply_markup=None,
        )
        msg_id = callback.message.message_id
    except Exception:
        data = await state.get_data()
        await _delete_messages(callback.bot, callback.message.chat.id, data.get("msg_to_delete", []))
        sent = await callback.message.answer("✍️ Напишите комментарий для водителя.\nНапример: Подъезд 2.")
        msg_id = sent.message_id
    await state.update_data(msg_to_delete=[msg_id])
    await state.set_state(OrderTaxi.waiting_for_comment)


@router.callback_query(F.data == "calculate_order_price", OrderTaxi.waiting_for_options)
async def calculate_order_price_callback(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    destination_addresses = data.get("destination_addresses") or []
    if not destination_addresses:
        await callback.answer("Сначала добавьте хотя бы один адрес назначения.", show_alert=True)
        return
    calculated_price, price_note, is_intercity = await _estimate_order_price(data)
    await state.update_data(calculated_price=calculated_price, price_note=price_note, is_intercity=is_intercity)
    summary_data = {**data, "calculated_price": calculated_price, "price_note": price_note}
    preorder_time_str = data.get("preorder_time_str")
    await callback.answer()
    summary_message_id = callback.message.message_id
    try:
        await callback.message.edit_text(
            _build_final_summary_text(summary_data),
            reply_markup=keyboards.get_order_confirmation_keyboard(preorder_time_str=preorder_time_str),
            parse_mode="HTML",
        )
    except Exception:
        sent = await callback.message.answer(
            _build_final_summary_text(summary_data),
            reply_markup=keyboards.get_order_confirmation_keyboard(preorder_time_str=preorder_time_str),
            parse_mode="HTML",
        )
        summary_message_id = sent.message_id
    await add_to_messages_to_delete(state, summary_message_id)
    await state.update_data(
        msg_to_delete=[summary_message_id],
        last_summary_message_id=summary_message_id,
    )
    await state.set_state(OrderTaxi.waiting_for_confirmation)


@router.callback_query(F.data == "confirm_order_creation", OrderTaxi.waiting_for_confirmation)
async def confirm_order_creation_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    msg_ids = data.get("messages_to_delete", [])
    chat_id = callback.message.chat.id
    for msg_id in msg_ids:
        try:
            await callback.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass
    await state.update_data(messages_to_delete=[])
    await finalize_order(callback.message, state, requester_telegram_id=callback.from_user.id)


@router.callback_query(F.data == "cancel_order_creation", OrderTaxi.waiting_for_to_address)
@router.callback_query(F.data == "cancel_order_creation", OrderTaxi.waiting_for_options)
@router.callback_query(F.data == "cancel_order_creation", OrderTaxi.waiting_for_confirmation)
async def cancel_order_creation_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    msg_ids = data.get("messages_to_delete", [])
    chat_id = callback.message.chat.id
    for msg_id in msg_ids:
        try:
            await callback.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass
    await perform_cleanup(callback.bot, callback.message.chat.id, state)
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass
    await state.clear()
    sent = await callback.message.answer(
        "Заказ отменен. Вы можете начать новый заказ в любое время.",
        reply_markup=keyboards.get_start_order_inline_keyboard(),
    )
    await state.update_data(last_bot_msg_id=sent.message_id)


async def _handle_offer_timeout(bot, order_id: int, driver_tg_id: int, timeout: int):
    """Таймер: если водитель не ответил — штраф и передача следующему."""
    await asyncio.sleep(timeout)
    if pending_offers.get(order_id) != driver_tg_id:
        return  # уже принято или отменено
    pending_offers.pop(order_id, None)
    offer_tasks.pop(order_id, None)
    # Штраф
    try:
        await get_http_client().post(f"/taxi/driver/{driver_tg_id}/penalty")
        await bot.send_message(
            chat_id=driver_tg_id,
            text=f"⏰ Время вышло! С вашего баланса списано {PENALTY_AMOUNT:.0f} руб. за пропуск заказа.",
        )
    except Exception as e:
        logger.error(f"Failed to apply timeout penalty to driver {driver_tg_id}: {e}")
    # В конец очереди
    if driver_tg_id in driver_queue:
        driver_queue.remove(driver_tg_id)
        driver_queue.append(driver_tg_id)
    # Предложить следующему
    asyncio.create_task(_offer_order_to_next(bot, order_id))


async def _offer_order_to_next(bot, order_id: int):
    """Предложить заказ следующему водителю в очереди."""
    order_info = pending_order_data.get(order_id)
    if not order_info:
        return
    driver_msg = order_info["driver_msg"]
    is_intercity = order_info.get("is_intercity", False)
    timeout = OFFER_TIMEOUT_INTERCITY if is_intercity else OFFER_TIMEOUT_CITY

    # Найти первого свободного водителя
    designated = None
    busy_ids = set(pending_offers.values())
    for drv_id in driver_queue:
        if drv_id not in busy_ids:
            designated = drv_id
            break

    if designated is None:
        return  # все заняты или очередь пуста

    pending_offers[order_id] = designated

    try:
        await bot.send_message(
            chat_id=designated,
            text=f"⏱ <b>У вас {timeout} сек. на принятие заказа!</b>\n\n" + driver_msg,
            reply_markup=keyboards.get_accept_order_keyboard(order_id),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Failed to send queue offer to driver {designated}: {e}")
        pending_offers.pop(order_id, None)
        if designated in driver_queue:
            driver_queue.remove(designated)
            driver_queue.append(designated)
        asyncio.create_task(_offer_order_to_next(bot, order_id))
        return

    task = asyncio.create_task(_handle_offer_timeout(bot, order_id, designated, timeout))
    offer_tasks[order_id] = task


async def _preorder_notify_task(
    bot, order_id: int, client_tg_id: int, driver_msg: str, is_intercity: bool, delay: float
):
    """Ждёт до времени предзаказа, затем уведомляет водителей."""
    if delay > 0:
        await asyncio.sleep(delay)
    preorder_tasks.pop(order_id, None)
    print(f"[PREORDER] Время пришло! Запускаем заказ #{order_id}", flush=True)
    # Уведомляем клиента
    try:
        await bot.send_message(
            chat_id=client_tg_id,
            text=f"🔔 Ваш предзаказ #{order_id} запущен! Ищем водителя...",
        )
    except Exception as e:
        logger.error(f"Preorder client notify error: {e}")
    # Отправляем в чат водителей
    try:
        await bot.send_message(
            settings.DRIVER_CHAT_ID,
            driver_msg,
            reply_markup=keyboards.get_accept_order_keyboard(order_id),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Preorder driver chat error: {e}")
    # Запускаем очередь
    pending_order_data[order_id] = {"driver_msg": driver_msg, "is_intercity": is_intercity}
    asyncio.create_task(_offer_order_to_next(bot, order_id))


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
    preorder_scheduled_at = data.get("preorder_scheduled_at")
    preorder_time_str = data.get("preorder_time_str")

    await state.update_data(is_processing=True)

    try:
        order_payload: dict = {
            "telegram_id": passenger_telegram_id,
            "from_address": from_address,
            "to_address": route_display,
            "comment": final_comment,
            "price": calculated_price,
        }
        if preorder_scheduled_at:
            order_payload["scheduled_at"] = preorder_scheduled_at
        response = await get_http_client().post("/taxi/order", json=order_payload)
            
        if response.status_code == 200:
            order = response.json()
            order_id = order['id']
            await state.update_data(order_id=order_id)

            # Формируем сообщение водителям (используется и сразу, и для предзаказа)
            data = await state.get_data()
            is_intercity = data.get("is_intercity", False)
            driver_msg_header = (
                f"🚕 <b>Новый заказ #{order_id}</b>"
                + (f" 🕐 <i>(предзаказ на {preorder_time_str})</i>" if preorder_time_str else "")
                + f"\n\n<b>{route_vertical}</b>"
            )
            _opts = []
            if data.get("has_child_seat"):
                _opts.append("👶 Детское кресло")
            if data.get("has_pet"):
                _opts.append("🐾 С питомцем")
            driver_msg = driver_msg_header
            if _opts:
                driver_msg += "\n\n" + "\n".join(_opts)
            if data.get("order_comment"):
                driver_msg += f"\n\n💬 <b>Комментарий:</b>\n<i>{data.get('order_comment')}</i>"
            if isinstance(calculated_price, (int, float)):
                driver_msg += f"\n\n💰 Стоимость: <b>{calculated_price:.0f} руб.</b>"

            if preorder_scheduled_at:
                # ── ПРЕДЗАКАЗ: уведомляем клиента и планируем задачу ──
                base_text = (
                    f"🕐 <b>Предзаказ на {preorder_time_str} создан!</b>\n\n"
                    f"{route_vertical}"
                )
                if isinstance(calculated_price, (int, float)):
                    base_text += f"\n\n💰 Стоимость: {calculated_price:.0f} руб."
                if final_comment:
                    base_text += f"\n\n💬 Примечание: {final_comment}"

                sent_msg = await message.answer(base_text, reply_markup=keyboards.get_order_manage_keyboard(order_id))
                await state.update_data(
                    msg_to_delete=[sent_msg.message_id],
                    active_message_id=sent_msg.message_id,
                    main_card_id=sent_msg.message_id,
                )

                # Планируем задачу на нужное время
                now = datetime.datetime.now(_SAMARA_TZ)
                scheduled_dt = datetime.datetime.fromisoformat(preorder_scheduled_at)
                delay = max(0.0, (scheduled_dt - now).total_seconds())
                print(f"[PREORDER] Заказ #{order_id} на {preorder_time_str}, delay={delay:.0f}s", flush=True)
                task = asyncio.create_task(
                    _preorder_notify_task(
                        message.bot, order_id, passenger_telegram_id,
                        driver_msg, is_intercity, delay,
                    )
                )
                preorder_tasks[order_id] = task
            else:
                # ── ОБЫЧНЫЙ ЗАКАЗ: отправляем водителям сразу ──
                base_text = (
                    "✅ Заказ создан! Ищем водителя...\n\n"
                    f"{route_vertical}"
                )
                if isinstance(calculated_price, (int, float)):
                    base_text += f"\n\n💰 Стоимость: {calculated_price:.0f} руб."
                if final_comment:
                    base_text += f"\n\n💬 Примечание: {final_comment}"

                sent_msg = await message.answer(base_text, reply_markup=keyboards.get_order_manage_keyboard(order_id))
                await add_to_messages_to_delete(state, sent_msg.message_id)
                await state.update_data(
                    msg_to_delete=[sent_msg.message_id],
                    active_message_id=sent_msg.message_id,
                    main_card_id=sent_msg.message_id,
                )

                try:
                    await message.bot.send_message(
                        settings.DRIVER_CHAT_ID,
                        driver_msg,
                        reply_markup=keyboards.get_accept_order_keyboard(order_id),
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logger.error(f"Ошибка отправки в чат водителей ({settings.DRIVER_CHAT_ID}): {e}")
                pending_order_data[order_id] = {"driver_msg": driver_msg, "is_intercity": is_intercity}
                asyncio.create_task(_offer_order_to_next(message.bot, order_id))

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

            # Отменяем таймер очереди — заказ принят
            if order_id in offer_tasks:
                offer_tasks[order_id].cancel()
                del offer_tasks[order_id]
            pending_offers.pop(order_id, None)
            pending_order_data.pop(order_id, None)

            await callback.answer("Вы приняли заказ! ✅")
            try:
                await callback.message.edit_text(
                    f"🚕 Заказ #{order_id} принят водителем {callback.from_user.full_name}.",
                    reply_markup=None,
                )
            except Exception:
                pass

            # Карточка заказа в личку водителю
            try:
                route_text = _format_route_from_values(order.get("from_address"), order.get("to_address"))
                client_name = order.get("client_name") or "Клиент"
                card_text = (
                    f"🚕 <b>Вы приняли заказ #{order_id}</b>\n\n"
                    f"👤 <b>Клиент:</b> {client_name}\n\n"
                    f"<b>{route_text}</b>"
                    + (f"\n\n💬 <b>Примечание:</b>\n    {order.get('comment')}" if order.get('comment') else "")
                )
                await callback.bot.send_message(
                    chat_id=driver_telegram_id,
                    text=card_text,
                    parse_mode="HTML",
                    reply_markup=keyboards.get_driver_accept_keyboard(
                        order_id,
                        order["from_address"],
                        client_telegram_id=order.get("client_telegram_id"),
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
                    await delete_messages_and_clear(callback.bot, client_chat_id, p_state)
                    pdata = await p_state.get_data()
                    main_card_id = pdata.get("main_card_id") or pdata.get("active_message_id")
                    edited = False
                    if isinstance(main_card_id, int):
                        try:
                            await callback.bot.edit_message_text(
                                chat_id=client_chat_id,
                                message_id=main_card_id,
                                text=text,
                                reply_markup=keyboards.get_client_after_accept_keyboard(order_id, driver_tg_id),
                            )
                            edited = True
                            await p_state.update_data(msg_to_delete=[main_card_id])
                        except Exception:
                            pass
                    if not edited:
                        if isinstance(main_card_id, int):
                            try:
                                await callback.bot.delete_message(chat_id=client_chat_id, message_id=main_card_id)
                            except Exception:
                                pass
                        sent_msg = await callback.bot.send_message(
                            client_chat_id,
                            text,
                            reply_markup=keyboards.get_client_after_accept_keyboard(order_id, driver_tg_id),
                        )
                        await p_state.update_data(
                            active_message_id=sent_msg.message_id,
                            main_card_id=sent_msg.message_id,
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

                    main_card_id = p_data.get("main_card_id") or p_data.get("active_message_id")
                    notification_id = p_data.get("notification_id")
                    if isinstance(notification_id, int):
                        await add_to_messages_to_delete(p_state, notification_id)
                    mid = p_data.get("last_new_order_prompt_id")
                    if isinstance(mid, int):
                        await add_to_messages_to_delete(p_state, mid)
                    await delete_messages_and_clear(callback.bot, client_chat_id, p_state)
                    edited = False
                    if isinstance(main_card_id, int):
                        try:
                            await callback.bot.edit_message_text(
                                chat_id=client_chat_id,
                                message_id=main_card_id,
                                text=receipt_text,
                                reply_markup=keyboards.get_rate_trip_keyboard(order_id),
                            )
                            edited = True
                        except Exception:
                            pass
                    if not edited:
                        if isinstance(main_card_id, int):
                            try:
                                await callback.bot.delete_message(chat_id=client_chat_id, message_id=main_card_id)
                            except Exception:
                                pass
                        receipt_msg = await callback.bot.send_message(
                            chat_id=client_chat_id,
                            text=receipt_text,
                            reply_markup=keyboards.get_rate_trip_keyboard(order_id),
                        )
                        main_card_id = receipt_msg.message_id

                    logger.info("Sending complete notification to passenger %s for order %s", client_chat_id, order_id)
                except Exception as e:
                    logger.exception(f"Error sending 'trip completed' (receipt) message to client {client_chat_id}: {e}")

            await callback.answer("Заказ завершен! Отличная работа 👍")
            try:
                await callback.message.delete()
            except Exception:
                pass
            price = data.get("price")
            price_str = f"{price:.0f} руб." if price is not None else "—"
            # Списать 5% комиссии с водителя
            commission_text = ""
            try:
                comm_resp = await get_http_client().post(f"/taxi/order/{order_id}/deduct_commission")
                if comm_resp.status_code == 200:
                    cd = comm_resp.json()
                    commission_text = (
                        f"\n📊 Комиссия (5%): -{cd['amount']:.2f} руб."
                        f"\n💼 Ваш баланс: {cd['new_balance']:.2f} руб."
                    )
            except Exception as e:
                logger.error(f"Failed to deduct commission for order {order_id}: {e}")
            await callback.bot.send_message(
                callback.from_user.id,
                f"✅ Заказ №{order_id} успешно завершен!\n💰 Сумма к оплате: {price_str}{commission_text}",
            )

            # После завершения заказа просим водителя указать актуальную стоянку
            await state.set_state(DriverShift.waiting_for_district)
            await callback.bot.send_message(
                chat_id=callback.from_user.id,
                text="<b>Выберите вашу текущую локацию (стоянку):</b>",
                reply_markup=keyboards.get_driver_districts_keyboard(),
                parse_mode="HTML",
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
        data = await state.get_data()
        msg_ids = data.get("messages_to_delete", [])
        chat_id = callback.message.chat.id
        for msg_id in msg_ids:
            try:
                await callback.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass
        await state.update_data(messages_to_delete=[])
        await state.clear()
        try:
            await callback.message.delete()
        except TelegramBadRequest:
            pass
        sent = await callback.message.answer(
            "Заказ отменен. Если вы хотите заказать такси снова, нажмите кнопку ниже.",
            reply_markup=keyboards.get_start_order_inline_keyboard(),
        )
        await state.update_data(last_menu_msg_id=sent.message_id)
        return

    order_id = int(state_order_id) if state_order_id is not None else int(raw_order_id)
    
    try:
        response = await get_http_client().post(
            f"/taxi/order/{order_id}/cancel",
            params={"telegram_id": callback.from_user.id}
        )
        
        if response.status_code == 200:
            data = await state.get_data()
            msg_ids = data.get("messages_to_delete", [])
            chat_id = callback.message.chat.id
            for msg_id in msg_ids:
                try:
                    await callback.bot.delete_message(chat_id=chat_id, message_id=msg_id)
                except Exception:
                    pass
            await state.update_data(messages_to_delete=[])
            await state.clear()
            await callback.answer("Заказ отменен ❌")
            try:
                await callback.message.edit_text(
                    f"❌ Заказ #{order_id} был отменен вами.\n\nЕсли вы хотите заказать такси снова, нажмите кнопку ниже.",
                    reply_markup=keyboards.get_start_order_inline_keyboard(),
                )
            except Exception:
                sent = await callback.message.answer(
                    "Заказ отменен. Если вы хотите заказать такси снова, нажмите кнопку ниже.",
                    reply_markup=keyboards.get_start_order_inline_keyboard(),
                )
                await state.update_data(last_menu_msg_id=sent.message_id)
        else:
            if state_order_id is None:
                data = await state.get_data()
                msg_ids = data.get("messages_to_delete", [])
                chat_id = callback.message.chat.id
                for msg_id in msg_ids:
                    try:
                        await callback.bot.delete_message(chat_id=chat_id, message_id=msg_id)
                    except Exception:
                        pass
                await state.update_data(messages_to_delete=[])
                await state.clear()
                try:
                    await callback.message.delete()
                except TelegramBadRequest:
                    pass
                sent = await callback.message.answer(
                    "Заказ отменен. Если вы хотите заказать такси снова, нажмите кнопку ниже.",
                    reply_markup=keyboards.get_start_order_inline_keyboard(),
                )
                await state.update_data(last_menu_msg_id=sent.message_id)
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
    order_id = int(callback.data.split("_")[-1])
    driver_tg_id = callback.from_user.id

    if pending_offers.get(order_id) == driver_tg_id:
        # Это назначенный водитель — отменяем таймер, применяем штраф
        if order_id in offer_tasks:
            offer_tasks[order_id].cancel()
            del offer_tasks[order_id]
        pending_offers.pop(order_id, None)
        # В конец очереди
        if driver_tg_id in driver_queue:
            driver_queue.remove(driver_tg_id)
            driver_queue.append(driver_tg_id)
        # Штраф
        try:
            await get_http_client().post(f"/taxi/driver/{driver_tg_id}/penalty")
            await callback.answer(f"Заказ отклонён. Списано {PENALTY_AMOUNT:.0f} руб.", show_alert=True)
        except Exception as e:
            logger.error(f"Failed to apply penalty for ignore: {e}")
            await callback.answer("Заказ отклонён.")
        # Предложить следующему
        asyncio.create_task(_offer_order_to_next(callback.bot, order_id))
    else:
        await callback.answer("Заказ скрыт")

    try:
        await callback.message.delete()
    except Exception:
        pass

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
                    notification_id = pdata.get("notification_id")
                    edited = False
                    if isinstance(notification_id, int):
                        try:
                            await callback.bot.edit_message_text(
                                chat_id=client_chat_id,
                                message_id=notification_id,
                                text=text,
                                reply_markup=keyboards.get_client_at_place_keyboard(order_id, driver_telegram_id),
                            )
                            edited = True
                        except Exception:
                            pass
                    if not edited:
                        if isinstance(notification_id, int):
                            try:
                                await callback.bot.delete_message(chat_id=client_chat_id, message_id=notification_id)
                            except Exception:
                                pass
                        sent = await callback.bot.send_message(
                            client_chat_id,
                            text,
                            reply_markup=keyboards.get_client_at_place_keyboard(order_id, driver_telegram_id),
                        )
                        await p_state.update_data(notification_id=sent.message_id)
            except Exception as e:
                logger.exception(f"Error sending 'at place' message to client {client_chat_id}: {e}")

            # Сообщение водителю
            await callback.answer("Вы отметили, что на месте ✅")
            try:
                await callback.message.edit_text(
                    f"🚕 Вы на месте по заказу #{order_id}.\n\n"
                    "<b>Ожидаем клиента. Нажмите «Начать поездку», когда он сядет в машину.</b>",
                    reply_markup=keyboards.get_at_place_driver_keyboard(order_id, client_chat_id),
                    parse_mode="HTML",
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
                    "<b>🚕 ПОЕЗДКА НАЧАТА.</b>\n\n"
                    "Ожидайте указаний по маршруту и следуйте к точке назначения.",
                    reply_markup=keyboards.get_in_progress_driver_keyboard(order_id, to_address),
                    parse_mode="HTML",
                )
            except Exception:
                pass
            await callback.answer("Поездка начата 🚕")

            # Уведомляем клиента: EDIT верхнего блока (добавить Поддержка) + нижнего («Поехали!» без кнопок)
            if client_chat_id is not None:
                try:
                    me = await callback.bot.get_me()
                    if client_chat_id == me.id:
                        logger.warning("Ошибка: попытка отправить сообщение боту")
                        return
                    p_state = _get_passenger_state(callback.bot, state.storage, client_chat_id)
                    p_data = await p_state.get_data()
                    main_card_id = p_data.get("main_card_id") or p_data.get("active_message_id")
                    driver_tg_id = data.get("driver_telegram_id") or callback.from_user.id
                    if isinstance(main_card_id, int):
                        try:
                            await callback.bot.edit_message_reply_markup(
                                chat_id=client_chat_id,
                                message_id=main_card_id,
                                reply_markup=keyboards.get_client_in_progress_keyboard(order_id, driver_tg_id),
                            )
                        except Exception:
                            pass
                    notification_id = p_data.get("notification_id")
                    edited = False
                    if isinstance(notification_id, int):
                        try:
                            await callback.bot.edit_message_text(
                                chat_id=client_chat_id,
                                message_id=notification_id,
                                text="🚀 Поехали! Приятного пути.",
                                reply_markup=None,
                            )
                            edited = True
                        except Exception:
                            pass
                    if not edited:
                        if isinstance(notification_id, int):
                            try:
                                await callback.bot.delete_message(chat_id=client_chat_id, message_id=notification_id)
                            except Exception:
                                pass
                        sent = await callback.bot.send_message(
                            client_chat_id,
                            "🚀 Поехали! Приятного пути.",
                        )
                        await p_state.update_data(notification_id=sent.message_id)
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
async def rate_trip_callback(callback: CallbackQuery, state: FSMContext):
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

    # Восстанавливаем главное меню с инлайн-кнопкой «Заказать такси»
    try:
        await state.clear()
        sent = await callback.message.answer(
            "Нажмите кнопку ниже, чтобы заказать такси снова.",
            reply_markup=keyboards.get_start_order_inline_keyboard(),
        )
        await state.update_data(last_menu_msg_id=sent.message_id)
    except Exception as e:
        logger.error(f"Failed to send menu after rating for {callback.from_user.id}: {e}")

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
