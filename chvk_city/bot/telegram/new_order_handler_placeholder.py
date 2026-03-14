from aiogram import Router, F
from aiogram.types import CallbackQuery
from aiogram.fsm.context import FSMContext

from . import keyboards
from .handlers import OrderTaxi  # переиспользуем состояние из основного файла

router = Router()


@router.callback_query(F.data == "start_new_order")
async def start_new_order_callback(callback: CallbackQuery, state: FSMContext):
    """
    Обработка кнопки '🚕 Заказать новое такси' после оценки.
    Сбрасываем состояние и переводим пользователя к вводу адреса 'откуда'.
    """
    await state.clear()
    await callback.message.edit_text(
        "Отлично! Куда поедем? Напишите адрес, откуда вас забрать: 📍",
    )
    await state.set_state(OrderTaxi.waiting_for_from_address)

