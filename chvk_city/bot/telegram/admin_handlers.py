from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

from chvk_city.bot.telegram.constants import ADMIN_IDS
from chvk_city.bot.telegram import keyboards
from chvk_city.bot.telegram.handlers import get_http_client

admin_router = Router()

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


@admin_router.callback_query(F.data == "admin_archive_noop")
async def admin_archive_noop(callback: CallbackQuery):
    await callback.answer()


@admin_router.callback_query(F.data == "admin_back")
async def admin_back_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer()
        return
    await callback.message.edit_text(
        "⚙️ Панель управления ЧВК Такси",
        reply_markup=keyboards.get_admin_panel_inline_keyboard(),
    )
    await callback.answer()
