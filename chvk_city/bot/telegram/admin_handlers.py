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
}


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
        lines = ["📋 <b>Текущие заказы:</b>\n"]
        for o in orders:
            from_addr = (o.get("from_address") or "—")[:40]
            to_addr = (o.get("to_address") or "—").split("\n")[0][:40]
            status = STATUS_LABELS.get(o.get("status", ""), o.get("status", "—"))
            price = f"{o['price']:.0f} руб." if o.get("price") else "—"
            lines.append(
                f"🆔 {o['id']} | {status}\n"
                f"  📍 {from_addr}\n"
                f"  🏁 {to_addr}\n"
                f"  💰 {price}"
            )
        text = "\n\n".join(lines)

    await callback.message.edit_text(
        text,
        reply_markup=keyboards.get_admin_back_keyboard(),
        parse_mode="HTML",
    )


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
