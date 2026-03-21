from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

from chvk_city.bot.telegram.constants import ADMIN_IDS
from chvk_city.bot.telegram import keyboards
from chvk_city.bot.telegram.handlers import get_http_client

admin_router = Router()


class AdminAddDriver(StatesGroup):
    waiting_for_tg_id = State()
    waiting_for_name = State()
    waiting_for_car_model = State()
    waiting_for_car_color = State()
    waiting_for_car_number = State()
    waiting_for_phone = State()
    waiting_for_confirm = State()

STATUS_LABELS = {
    "new": "🟡 Ожидает",
    "accepted": "🟢 Принят",
    "at_place": "🚗 На месте",
    "in_progress": "🔵 В пути",
    "completed": "✅ Завершён",
    "cancelled": "❌ Отменён",
}


def _format_order_card(o: dict) -> str:
    status = STATUS_LABELS.get(o.get("status", ""), o.get("status", "—"))
    from_addr = (o.get("from_address") or "—")[:50]
    to_addr = (o.get("to_address") or "—").split("\n")[0][:50]
    price = f"{o['price']:.0f} руб." if o.get("price") else "—"

    client_tg = o.get("client_tg_id")
    client_name = o.get("client_name") or "Клиент"
    client_line = (
        f'<a href="tg://user?id={client_tg}">{client_name}</a>'
        if client_tg else client_name
    )

    driver_tg = o.get("driver_tg_id")
    if driver_tg:
        driver_name = o.get("driver_name") or "Водитель"
        driver_phone = o.get("driver_phone") or ""
        car = f"{o.get('car_model', '')} {o.get('car_number', '')}".strip()
        parts = [f'<a href="tg://user?id={driver_tg}">{driver_name}</a>']
        if driver_phone:
            parts.append(driver_phone)
        if car:
            parts.append(car)
        driver_line = " | ".join(parts)
    else:
        driver_line = "🔍 Поиск..." if o.get("status") == "new" else "Не назначен"

    created = o.get("created_at", "")
    date_line = f"🕐 {created}\n" if created else ""

    return (
        f"\n<b>Заказ №{o['id']}</b> — {status}\n"
        f"{date_line}"
        f"📍 Откуда: {from_addr}\n"
        f"🏁 Куда: {to_addr}\n"
        f"💰 Стоимость: {price}\n"
        f"👤 Клиент: {client_line}\n"
        f"🚕 Водитель: {driver_line}"
    )


@admin_router.message(F.text == "⚙️ Управление")
async def admin_panel_handler(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer(
        "⚙️ Панель управления ЧВК Такси",
        reply_markup=keyboards.get_admin_panel_inline_keyboard(),
    )


@admin_router.callback_query(F.data == "open_admin_panel")
async def open_admin_panel_callback(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.message.edit_text(
        "⚙️ Панель управления ЧВК Такси",
        reply_markup=keyboards.get_admin_panel_inline_keyboard(),
    )
    await callback.answer()


@admin_router.callback_query(F.data == "admin_current_orders")
async def admin_current_orders_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return

    await callback.answer()

    try:
        resp = await get_http_client().get("/taxi/orders/active")
        orders = resp.json() if resp.status_code == 200 else []
    except Exception:
        orders = []

    if not orders:
        text = "📋 Активных заказов нет."
    else:
        cards = ["📋 <b>Текущие заказы:</b>"]
        for o in orders:
            cards.append(_format_order_card(o))
        text = "\n".join(cards)

    await callback.message.edit_text(
        text,
        reply_markup=keyboards.get_admin_back_keyboard(),
        parse_mode="HTML",
    )


@admin_router.callback_query(F.data.startswith("admin_archive_page:"))
async def admin_archive_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return

    page = int(callback.data.split(":")[1])

    try:
        resp = await get_http_client().get("/taxi/orders/archive", params={"page": page})
        data = resp.json() if resp.status_code == 200 else {}
    except Exception:
        data = {}

    orders = data.get("orders", [])
    has_next = data.get("has_next", False)
    has_prev = page > 0

    if not orders:
        if page == 0:
            text = "📦 Архив пуст."
        else:
            await callback.answer("Это последняя страница.", show_alert=True)
            return
    else:
        cards = [f"📦 <b>Архив заказов</b> — стр. {page + 1}:"]
        for o in orders:
            cards.append(_format_order_card(o))
        text = "\n".join(cards)

    await callback.message.edit_text(
        text,
        reply_markup=keyboards.get_admin_archive_keyboard(page, has_prev, has_next),
        parse_mode="HTML",
    )
    await callback.answer()


@admin_router.callback_query(F.data == "admin_drivers_menu")
async def admin_drivers_menu_callback(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return
    await _delete_driver_cards(callback.bot, callback.message.chat.id, state)
    # Редактируем то сообщение, с которого пришёл callback (футер или любое другое)
    try:
        await callback.message.edit_text(
            "🚕 Управление водителями",
            reply_markup=keyboards.get_admin_drivers_menu_keyboard(),
        )
    except TelegramBadRequest:
        await callback.message.answer(
            "🚕 Управление водителями",
            reply_markup=keyboards.get_admin_drivers_menu_keyboard(),
        )
    await callback.answer()


async def _delete_driver_cards(bot, chat_id: int, state: FSMContext):
    """Удаляем все сохранённые сообщения-карточки водителей."""
    data = await state.get_data()
    card_ids: list[int] = data.get("admin_driver_card_ids", [])
    for msg_id in card_ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except TelegramBadRequest:
            pass
    await state.update_data(admin_driver_card_ids=[])


@admin_router.callback_query(F.data == "admin_drivers_active")
async def admin_drivers_active_callback(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return

    await callback.answer()

    # Удаляем старые карточки если они ещё висят
    await _delete_driver_cards(callback.bot, callback.message.chat.id, state)

    try:
        resp = await get_http_client().get("/taxi/drivers/all")
        drivers = resp.json() if resp.status_code == 200 else []
    except Exception:
        drivers = []

    if not drivers:
        await callback.message.edit_text(
            "🚕 Действующих водителей нет.",
            reply_markup=keyboards.get_admin_drivers_menu_keyboard(),
        )
        return

    text_header = f"🚕 <b>Действующие водители</b> ({len(drivers)}):"
    await callback.message.edit_text(text_header, reply_markup=None, parse_mode="HTML")

    card_ids: list[int] = []
    for d in drivers:
        tg_id = d.get("telegram_id")
        name = d.get("name") or "—"
        phone = d.get("phone") or "—"
        car = f"{d.get('car_model', '')} {d.get('car_number', '')}".strip() or "—"
        district = d.get("current_district") or "—"
        profile = f'<a href="tg://user?id={tg_id}">Профиль в TG</a>' if tg_id else "—"

        card = (
            f"👤 <b>{name}</b>\n"
            f"📞 Тел: {phone}\n"
            f"🚗 Машина: {car}\n"
            f"📍 Район: {district}\n"
            f"🔗 {profile}"
        )
        sent = await callback.message.answer(
            card,
            reply_markup=keyboards.get_admin_driver_card_keyboard(d["id"]),
            parse_mode="HTML",
        )
        card_ids.append(sent.message_id)

    footer = await callback.message.answer(
        "⬆️ Список водителей",
        reply_markup=keyboards.get_admin_drivers_menu_keyboard(),
    )

    # Сохраняем для удаления: заголовок + карточки (но НЕ футер — он редактируется)
    card_ids.append(callback.message.message_id)
    await state.update_data(
        admin_driver_card_ids=card_ids,
        admin_drivers_footer_id=footer.message_id,
    )


@admin_router.callback_query(F.data == "admin_drivers_requests")
async def admin_drivers_requests_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.answer("В разработке", show_alert=True)


@admin_router.callback_query(F.data.startswith("admin_delete_driver:"))
async def admin_delete_driver_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return

    driver_id = int(callback.data.split(":")[1])

    # Получаем имя водителя через список
    try:
        resp = await get_http_client().get("/taxi/drivers/all")
        drivers = resp.json() if resp.status_code == 200 else []
    except Exception:
        drivers = []

    driver = next((d for d in drivers if d["id"] == driver_id), None)
    name = driver.get("name") or "водителя" if driver else "водителя"

    await callback.message.edit_text(
        f"⚠️ Вы уверены, что хотите лишить <b>{name}</b> прав водителя?",
        reply_markup=keyboards.get_admin_driver_confirm_keyboard(driver_id, name),
        parse_mode="HTML",
    )
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin_confirm_delete:"))
async def admin_confirm_delete_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return

    driver_id = int(callback.data.split(":")[1])
    driver_tg_id = None

    try:
        resp = await get_http_client().post(f"/taxi/driver/{driver_id}/reject")
        print(f"DEBUG: Attempting to delete driver {driver_id}. Server response: {resp.status_code} - {resp.text}", flush=True)
        success = resp.status_code == 200
        if success:
            driver_tg_id = resp.json().get("telegram_id")
    except Exception as e:
        print(f"DEBUG: Exception during driver deletion: {e}", flush=True)
        success = False

    if success:
        await callback.message.edit_text(
            "✅ Водитель удалён из системы.",
            reply_markup=keyboards.get_admin_drivers_back_keyboard(),
        )
        # Уведомляем водителя
        if driver_tg_id:
            try:
                await callback.bot.send_message(
                    chat_id=driver_tg_id,
                    text="⚠️ Ваш статус водителя был аннулирован администратором.",
                )
            except Exception as e:
                print(f"DEBUG: Failed to notify driver {driver_tg_id}: {e}", flush=True)
    else:
        await callback.message.edit_text(
            "❌ Не удалось удалить водителя. Попробуйте снова.",
            reply_markup=keyboards.get_admin_drivers_back_keyboard(),
        )
    await callback.answer()


@admin_router.callback_query(F.data == "admin_archive_noop")
async def admin_archive_noop(callback: CallbackQuery):
    await callback.answer()


@admin_router.callback_query(F.data == "admin_back")
async def admin_back_callback(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer()
        return
    await _delete_driver_cards(callback.bot, callback.message.chat.id, state)
    await callback.message.edit_text(
        "⚙️ Панель управления ЧВК Такси",
        reply_markup=keyboards.get_admin_panel_inline_keyboard(),
    )
    await callback.answer()


@admin_router.callback_query(F.data == "admin_to_main")
async def admin_to_main_callback(callback: CallbackQuery):
    await callback.message.edit_text(
        "Привет! Я помогу вам заказать такси. Нажмите на кнопку ниже, чтобы начать.",
        reply_markup=keyboards.get_start_order_inline_keyboard_admin(),
    )
    await callback.answer()


# ── Добавить водителя (FSM, single-window) ───────────────────────────────────

_STEP_TEXTS = {
    "tg_id":      "➕ <b>Добавление водителя</b>\n\nШаг 1/6. Введите <b>Telegram ID</b> водителя (числовой):",
    "name":       "➕ <b>Добавление водителя</b>\n\nШаг 2/6. Введите <b>ФИО</b> водителя\n(Фамилия Имя Отчество):",
    "car_model":  "➕ <b>Добавление водителя</b>\n\nШаг 3/6. Введите <b>марку автомобиля</b>\n(например: Toyota Camry):",
    "car_color":  "➕ <b>Добавление водителя</b>\n\nШаг 4/6. Введите <b>цвет автомобиля</b>\n(например: белый, чёрный, серебристый):",
    "car_number": "➕ <b>Добавление водителя</b>\n\nШаг 5/6. Введите <b>номер автомобиля</b>\n(например: А123ВС159):",
    "phone":      "➕ <b>Добавление водителя</b>\n\nШаг 6/6. Введите <b>номер телефона</b> водителя\n(или «—» чтобы пропустить):",
}


async def _edit_reg_msg(bot, chat_id: int, msg_id: int, text: str, reply_markup=None):
    """Редактировать регистрационное сообщение, игнорируя 'message is not modified'."""
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode="HTML",
        )
    except TelegramBadRequest:
        pass


@admin_router.callback_query(F.data == "admin_add_driver_start")
async def admin_add_driver_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AdminAddDriver.waiting_for_tg_id)
    await state.update_data(registration_msg_id=callback.message.message_id)
    await callback.message.edit_text(
        _STEP_TEXTS["tg_id"],
        reply_markup=keyboards.get_admin_cancel_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@admin_router.message(StateFilter(AdminAddDriver.waiting_for_tg_id))
async def admin_add_driver_tg_id(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    text = (message.text or "").strip()
    data = await state.get_data()
    msg_id = data.get("registration_msg_id")
    try:
        await message.delete()
    except TelegramBadRequest:
        pass

    if not text.lstrip("-").isdigit():
        await _edit_reg_msg(
            message.bot, message.chat.id, msg_id,
            f"⚠️ <b>Ошибка:</b> Telegram ID должен быть числом. Попробуйте ещё раз:\n\n{_STEP_TEXTS['tg_id']}",
            keyboards.get_admin_cancel_keyboard(),
        )
        return

    await state.update_data(new_driver_tg_id=int(text))
    await state.set_state(AdminAddDriver.waiting_for_name)
    await _edit_reg_msg(
        message.bot, message.chat.id, msg_id,
        _STEP_TEXTS["name"],
        keyboards.get_admin_cancel_keyboard(),
    )


@admin_router.message(StateFilter(AdminAddDriver.waiting_for_name))
async def admin_add_driver_name(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    name = (message.text or "").strip()
    data = await state.get_data()
    msg_id = data.get("registration_msg_id")
    try:
        await message.delete()
    except TelegramBadRequest:
        pass

    if len(name) < 3:
        await _edit_reg_msg(
            message.bot, message.chat.id, msg_id,
            f"⚠️ <b>Ошибка:</b> ФИО должно содержать минимум 3 символа. Попробуйте ещё раз:\n\n{_STEP_TEXTS['name']}",
            keyboards.get_admin_cancel_keyboard(),
        )
        return

    await state.update_data(new_driver_name=name)
    await state.set_state(AdminAddDriver.waiting_for_car_model)
    await _edit_reg_msg(
        message.bot, message.chat.id, msg_id,
        _STEP_TEXTS["car_model"],
        keyboards.get_admin_cancel_keyboard(),
    )


@admin_router.message(StateFilter(AdminAddDriver.waiting_for_car_model))
async def admin_add_driver_car_model(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    car_model = (message.text or "").strip()
    data = await state.get_data()
    msg_id = data.get("registration_msg_id")
    try:
        await message.delete()
    except TelegramBadRequest:
        pass

    if not car_model:
        await _edit_reg_msg(
            message.bot, message.chat.id, msg_id,
            f"⚠️ <b>Ошибка:</b> Поле не может быть пустым. Попробуйте ещё раз:\n\n{_STEP_TEXTS['car_model']}",
            keyboards.get_admin_cancel_keyboard(),
        )
        return

    await state.update_data(new_driver_car_model=car_model)
    await state.set_state(AdminAddDriver.waiting_for_car_color)
    await _edit_reg_msg(
        message.bot, message.chat.id, msg_id,
        _STEP_TEXTS["car_color"],
        keyboards.get_admin_cancel_keyboard(),
    )


@admin_router.message(StateFilter(AdminAddDriver.waiting_for_car_color))
async def admin_add_driver_car_color(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    car_color = (message.text or "").strip()
    data = await state.get_data()
    msg_id = data.get("registration_msg_id")
    try:
        await message.delete()
    except TelegramBadRequest:
        pass

    if not car_color:
        await _edit_reg_msg(
            message.bot, message.chat.id, msg_id,
            f"⚠️ <b>Ошибка:</b> Поле не может быть пустым. Попробуйте ещё раз:\n\n{_STEP_TEXTS['car_color']}",
            keyboards.get_admin_cancel_keyboard(),
        )
        return

    await state.update_data(new_driver_car_color=car_color)
    await state.set_state(AdminAddDriver.waiting_for_car_number)
    await _edit_reg_msg(
        message.bot, message.chat.id, msg_id,
        _STEP_TEXTS["car_number"],
        keyboards.get_admin_cancel_keyboard(),
    )


@admin_router.message(StateFilter(AdminAddDriver.waiting_for_car_number))
async def admin_add_driver_car_number(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    car_number = (message.text or "").strip()
    data = await state.get_data()
    msg_id = data.get("registration_msg_id")
    try:
        await message.delete()
    except TelegramBadRequest:
        pass

    if not car_number:
        await _edit_reg_msg(
            message.bot, message.chat.id, msg_id,
            f"⚠️ <b>Ошибка:</b> Поле не может быть пустым. Попробуйте ещё раз:\n\n{_STEP_TEXTS['car_number']}",
            keyboards.get_admin_cancel_keyboard(),
        )
        return

    await state.update_data(new_driver_car_number=car_number)
    await state.set_state(AdminAddDriver.waiting_for_phone)
    await _edit_reg_msg(
        message.bot, message.chat.id, msg_id,
        _STEP_TEXTS["phone"],
        keyboards.get_admin_cancel_keyboard(),
    )


@admin_router.message(StateFilter(AdminAddDriver.waiting_for_phone))
async def admin_add_driver_phone(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    phone_raw = (message.text or "").strip()
    data = await state.get_data()
    msg_id = data.get("registration_msg_id")
    try:
        await message.delete()
    except TelegramBadRequest:
        pass

    phone = None if phone_raw in ("—", "-") else phone_raw

    await state.update_data(new_driver_phone=phone)
    await state.set_state(AdminAddDriver.waiting_for_confirm)

    tg_id = data["new_driver_tg_id"]
    name = data.get("new_driver_name") or "—"
    car_model = data["new_driver_car_model"]
    car_color = data.get("new_driver_car_color") or "—"
    car_number = data["new_driver_car_number"]
    phone_line = phone or "не указан"

    await _edit_reg_msg(
        message.bot, message.chat.id, msg_id,
        f"📋 <b>Подтвердите данные водителя:</b>\n\n"
        f"🆔 Telegram ID: <code>{tg_id}</code>\n"
        f"👤 ФИО: {name}\n"
        f"🚗 Машина: {car_model}\n"
        f"🎨 Цвет: {car_color}\n"
        f"🔢 Номер: {car_number}\n"
        f"📞 Телефон: {phone_line}",
        keyboards.get_admin_confirm_add_driver_keyboard(),
    )


@admin_router.callback_query(F.data == "admin_confirm_add_driver")
async def admin_confirm_add_driver(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return

    data = await state.get_data()
    tg_id = data.get("new_driver_tg_id")
    name = data.get("new_driver_name")
    car_model = data.get("new_driver_car_model")
    car_color = data.get("new_driver_car_color")
    car_number = data.get("new_driver_car_number")
    phone = data.get("new_driver_phone")

    await state.clear()

    try:
        payload = {"telegram_id": tg_id, "car_model": car_model, "car_number": car_number}
        if name:
            payload["name"] = name
        if car_color:
            payload["car_color"] = car_color
        if phone:
            payload["phone"] = phone
        resp = await get_http_client().post("/taxi/admin/add_driver", json=payload)
        success = resp.status_code == 200
    except Exception as e:
        print(f"DEBUG: add_driver error: {e}", flush=True)
        success = False

    if success:
        await callback.message.edit_text(
            f"✅ Водитель <code>{tg_id}</code> успешно добавлен и одобрен.",
            reply_markup=keyboards.get_admin_drivers_menu_keyboard(),
            parse_mode="HTML",
        )
        try:
            await callback.bot.send_message(
                chat_id=tg_id,
                text="🎉 Вы были добавлены в систему как водитель ЧВК Такси. Ваша заявка одобрена!",
            )
        except Exception:
            pass
    else:
        await callback.message.edit_text(
            "❌ Не удалось добавить водителя. Проверьте данные и попробуйте снова.",
            reply_markup=keyboards.get_admin_drivers_menu_keyboard(),
        )
    await callback.answer()


@admin_router.callback_query(F.data == "admin_add_driver_cancel")
async def admin_add_driver_cancel(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer()
        return
    await state.clear()
    try:
        await callback.message.edit_text(
            "🚕 Управление водителями",
            reply_markup=keyboards.get_admin_drivers_menu_keyboard(),
        )
    except TelegramBadRequest:
        await callback.message.answer(
            "🚕 Управление водителями",
            reply_markup=keyboards.get_admin_drivers_menu_keyboard(),
        )
    await callback.answer()


# ── Удалённые водители ───────────────────────────────────────────────────────

@admin_router.callback_query(F.data == "admin_drivers_deleted")
async def admin_drivers_deleted_callback(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return

    await callback.answer()
    await _delete_driver_cards(callback.bot, callback.message.chat.id, state)

    try:
        resp = await get_http_client().get("/taxi/drivers/deleted")
        drivers = resp.json() if resp.status_code == 200 else []
    except Exception:
        drivers = []

    if not drivers:
        await callback.message.edit_text(
            "🗑 Удалённых водителей нет.",
            reply_markup=keyboards.get_admin_drivers_menu_keyboard(),
        )
        return

    text_header = f"🗑 <b>Удалённые водители</b> ({len(drivers)}):"
    await callback.message.edit_text(text_header, reply_markup=None, parse_mode="HTML")

    card_ids: list[int] = []
    for d in drivers:
        tg_id = d.get("telegram_id")
        name = d.get("name") or "—"
        phone = d.get("phone") or "—"
        car = f"{d.get('car_model', '')} {d.get('car_number', '')}".strip() or "—"
        deleted_at = d.get("deleted_at") or "—"
        profile = f'<a href="tg://user?id={tg_id}">Профиль в TG</a>' if tg_id else "—"

        card = (
            f"👤 <b>{name}</b>\n"
            f"📞 Тел: {phone}\n"
            f"🚗 Машина: {car}\n"
            f"🗑 Удалён: {deleted_at}\n"
            f"🔗 {profile}"
        )
        sent = await callback.message.answer(card, parse_mode="HTML")
        card_ids.append(sent.message_id)

    footer = await callback.message.answer(
        "⬆️ Список удалённых водителей",
        reply_markup=keyboards.get_admin_drivers_menu_keyboard(),
    )

    card_ids.append(callback.message.message_id)
    await state.update_data(
        admin_driver_card_ids=card_ids,
        admin_drivers_footer_id=footer.message_id,
    )
