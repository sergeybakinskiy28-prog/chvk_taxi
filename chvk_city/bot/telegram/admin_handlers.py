from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

from chvk_city.bot.telegram.constants import ADMIN_IDS
from chvk_city.bot.telegram import keyboards

admin_router = Router()


@admin_router.message(F.text == "⚙️ Управление")
async def admin_panel_handler(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer(
        "⚙️ Панель управления ЧВК Такси",
        reply_markup=keyboards.get_admin_panel_inline_keyboard(),
    )


@admin_router.callback_query(F.data == "admin_current_orders")
async def admin_current_orders_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.answer("В разработке", show_alert=True)


@admin_router.callback_query(F.data == "admin_back")
async def admin_back_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer()
        return
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()
