import logging
import httpx
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import StorageKey  # 1) Импортируем StorageKey
from aiogram.utils.keyboard import InlineKeyboardBuilder
from chvk_city.backend.config import settings
from chvk_city.bot.telegram import keyboards

logger = logging.getLogger(__name__)
router = Router()

# Ленивое создание httpx-клиента, чтобы избежать утечек ресурсов
_http_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    """
    Возвращает общий AsyncClient для всех хендлеров.
    Создаётся при первом обращении.
    """
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            base_url=settings.API_BASE_URL,
            trust_env=False,
            timeout=10.0,
        )
    return _http_client

class OrderTaxi(StatesGroup):
    waiting_for_from_address = State()
    waiting_for_to_address = State()
    waiting_for_comment = State()

class RegisterDriver(StatesGroup):
    waiting_for_car_model = State()
    waiting_for_car_number = State()

# 2) Функция для получения состояния FSM пассажира по user_id и боту
def _get_passenger_state(bot, storage, user_id):
    key = StorageKey(
        bot_id=bot.id,
        chat_id=user_id,
        user_id=user_id,
    )
    return FSMContext(storage=storage, key=key)

# Регистрация водителей через бота отключена: водители добавляются вручную через БД или админку.
# Команда /driver и опрос (марка авто, госномер) удалены из общего доступа.

@router.message(CommandStart())
async def cmd_start(message: Message):
    """
    Стартовая команда:
    - если пользователь уже есть в БД и у него есть телефон — сразу показываем главное меню;
    - иначе регистрируем (при необходимости) и просим отправить контакт.
    """
    user_id = message.from_user.id

    # Пытаемся получить пользователя из БД
    try:
        resp = await get_http_client().get(f"/taxi/user/{user_id}")
        if resp.status_code == 200:
            user_data = resp.json()
            if user_data.get("phone"):
                # Телефон уже есть — сразу показываем главное меню
                await message.answer(
                    "Рады видеть вас здесь! Куда отправимся? 🚕",
                    reply_markup=keyboards.get_main_menu()
                )
                return
        # Если 404 или нет телефона — продолжаем обычный флоу
    except Exception as e:
        logger.error(f"Failed to fetch user {user_id}: {e}")

    # Регистрируем пользователя (если его ещё нет)
    try:
        await get_http_client().post(
            "/taxi/user/register",
            json={
                "telegram_id": user_id,
                "name": message.from_user.full_name
            }
        )
    except Exception as e:
        logger.error(f"Failed to register user {user_id}: {e}")
    
    await message.answer(
        "Рады видеть вас здесь! Куда отправимся? 🚕\n\n"
        "Для безопасности сервиса и возможности связи водителя с вами, пожалуйста, подтвердите ваш номер телефона, нажав кнопку ниже. 👇",
        reply_markup=keyboards.get_phone_keyboard()
    )

@router.message(F.contact)
async def process_contact(message: Message):
    phone = message.contact.phone_number
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
    
    await message.answer(
        f"✅ Номер {phone} подтвержден! Теперь вы можете заказать такси.",
        reply_markup=keyboards.get_main_menu()
    )

@router.message(F.text == "🚕 Заказать такси")
async def taxi_order_start(message: Message, state: FSMContext):
    # Перед началом нового заказа пытаемся удалить прошлое меню "Вы можете заказать такси снова:"
    data = await state.get_data()
    last_menu_id = data.get("last_menu_msg_id")
    if isinstance(last_menu_id, int):
        try:
            await message.bot.delete_message(chat_id=message.chat.id, message_id=last_menu_id)
            print(f"DEBUG: Удалено предыдущее меню last_menu_msg_id={last_menu_id}")
        except Exception as e:
            print(f"DEBUG: Не удалось удалить предыдущее меню {last_menu_id}: {e}")

    # Начинаем новый список сообщений для удаления
    msg_list: list[int] = []
    # Сохраняем само нажатие "🚕 Заказать такси" (сообщение пользователя)
    msg_list.append(message.message_id)

    sent = await message.answer("Откуда вас забрать? 📍")

    # Сохраняем первое сообщение бота ("Откуда вас забрать?") в msg_to_delete в состоянии пассажира
    msg_list.append(sent.message_id)
    print(f"DEBUG: Записываю ID {message.message_id} и {sent.message_id} в список на удаление (start_order), текущее: {msg_list}")
    await state.update_data(msg_to_delete=msg_list)

    await state.set_state(OrderTaxi.waiting_for_from_address)


@router.message(F.text == "🗂 Мои заказы")
async def my_orders_handler(message: Message):
    """Показ истории завершённых заказов пользователя."""
    telegram_id = message.from_user.id
    try:
        user_resp = await get_http_client().get(f"/taxi/user/{telegram_id}")
        if user_resp.status_code != 200:
            await message.answer(
                "Вы еще не совершали поездок. Самое время заказать такси! 🚕",
                reply_markup=keyboards.get_main_menu(),
            )
            return
        user_data = user_resp.json()
        user_id = user_data.get("id")
        if not user_id:
            await message.answer(
                "Вы еще не совершали поездок. Самое время заказать такси! 🚕",
                reply_markup=keyboards.get_main_menu(),
            )
            return

        history_resp = await get_http_client().get(f"/taxi/orders/history/{user_id}")
        if history_resp.status_code != 200:
            await message.answer(
                "Не удалось загрузить историю заказов. Попробуйте позже.",
                reply_markup=keyboards.get_main_menu(),
            )
            return

        orders = history_resp.json()
        if not orders:
            await message.answer(
                "Вы еще не совершали поездок. Самое время заказать такси! 🚕",
                reply_markup=keyboards.get_main_menu(),
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
            reply_markup=keyboards.get_main_menu(),
        )


@router.callback_query(F.data == "back_to_menu")
async def back_to_menu_callback(callback: CallbackQuery):
    """Возврат в главное меню из списка заказов или других экранов."""
    await callback.answer()
    await callback.message.answer(
        "Главное меню. Выберите действие:",
        reply_markup=keyboards.get_main_menu(),
    )


@router.message(OrderTaxi.waiting_for_from_address)
async def process_from_address(message: Message, state: FSMContext):
    # Сохраняем ответ пользователя (адрес "откуда") в список на удаление
    data = await state.get_data()
    msg_list = data.get("msg_to_delete", [])
    msg_list.append(message.message_id)
    print(f"DEBUG: Записываю ID {message.message_id} (ответ from_address) в список на удаление, текущее: {msg_list}")
    await state.update_data(msg_to_delete=msg_list)

    await state.update_data(from_address=message.text)
    sent = await message.answer("Куда ехать? 🏁")

    # Сохраняем сообщение "Куда ехать?" в msg_to_delete
    data = await state.get_data()
    msg_list = data.get("msg_to_delete", [])
    msg_list.append(sent.message_id)
    print(f"DEBUG: Записываю ID {sent.message_id} в список на удаление (process_from_address), текущее: {msg_list}")
    await state.update_data(msg_to_delete=msg_list)

    await state.set_state(OrderTaxi.waiting_for_to_address)

@router.message(OrderTaxi.waiting_for_to_address)
async def process_to_address(message: Message, state: FSMContext):
    # Сохраняем ответ пользователя (адрес "куда") в список на удаление
    data = await state.get_data()
    msg_list = data.get("msg_to_delete", [])
    msg_list.append(message.message_id)
    print(f"DEBUG: Записываю ID {message.message_id} (ответ to_address) в список на удаление, текущее: {msg_list}")
    await state.update_data(msg_to_delete=msg_list)

    await state.update_data(to_address=message.text)
    sent = await message.answer(
        "Желаете добавить комментарий к заказу? ✍️\n(Например: подъезд 2, детское кресло, багаж)\n\nИли нажмите кнопку ниже, чтобы пропустить. 👇",
        reply_markup=keyboards.get_skip_comment_keyboard()
    )

    # Сохраняем сообщение с предложением комментария в msg_to_delete
    data = await state.get_data()
    msg_list = data.get("msg_to_delete", [])
    msg_list.append(sent.message_id)
    print(f"DEBUG: Записываю ID {sent.message_id} в список на удаление (process_to_address), текущее: {msg_list}")
    await state.update_data(msg_to_delete=msg_list)

    await state.set_state(OrderTaxi.waiting_for_comment)

@router.callback_query(F.data == "skip_comment", OrderTaxi.waiting_for_comment)
async def process_skip_comment(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.delete()
    await finalize_order(callback.message, state, comment=None)

@router.message(OrderTaxi.waiting_for_comment)
async def process_comment(message: Message, state: FSMContext):
    # Сохраняем ответ пользователя (комментарий) в список на удаление
    data = await state.get_data()
    msg_list = data.get("msg_to_delete", [])
    msg_list.append(message.message_id)
    print(f"DEBUG: Записываю ID {message.message_id} (ответ comment) в список на удаление, текущее: {msg_list}")
    await state.update_data(msg_to_delete=msg_list)

    await finalize_order(message, state, comment=message.text)

async def finalize_order(message: Message, state: FSMContext, comment: str | None = None):
    data = await state.get_data()
    from_address = data.get('from_address')
    to_address = data.get('to_address')
    
    if not from_address or not to_address:
        await message.answer("❌ Ошибка: адрес отправления или назначения не указан.")
        await state.clear()
        return

    try:
        logger.debug(f"Creating order for user {message.from_user.id}: {from_address} -> {to_address}")
        response = await get_http_client().post(
            "/taxi/order",
            json={
                "telegram_id": message.from_user.id,
                "from_address": from_address,
                "to_address": to_address,
                "comment": comment
            }
        )
            
        if response.status_code == 200:
            order = response.json()
            order_id = order['id']
            base_text = (
                "✅ Заказ создан! Ищем водителя...\n\n"
                f"📍 Откуда: {from_address}\n"
                f"🏁 Куда: {to_address}"
            )
            if comment:
                base_text += f"\n\n💬 Примечание: {comment}"

            sent_msg = await message.answer(
                base_text,
                reply_markup=keyboards.get_order_manage_keyboard(order_id)
            )

            # 3) Сохраняем ID сообщения о заказе в список msg_to_delete пассажира
            data = await state.get_data()
            msg_list = data.get("msg_to_delete", [])
            msg_list.append(sent_msg.message_id)
            print(f"DEBUG: Записываю ID {sent_msg.message_id} (сообщение 'Заказ создан') в список на удаление, текущее: {msg_list}")
            await state.update_data(msg_to_delete=msg_list)

            # Отправка в чат водителей
            driver_msg = (
                f"🚕 **Новый заказ #{order_id}**\n\n"
                f"📍 Откуда: {from_address}\n"
                f"🏁 Куда: {to_address}"
            )
            if comment:
                driver_msg += f"\n\n💬 Комментарий: {comment}"
            
            try:
                await message.bot.send_message(
                    settings.DRIVER_CHAT_ID,
                    driver_msg,
                    reply_markup=keyboards.get_accept_order_keyboard(order_id)
                )
            except Exception as e:
                logger.error(f"Ошибка отправки в чат водителей ({settings.DRIVER_CHAT_ID}): {e}")
        else:
            error_detail = response.text
            logger.error(f"Ошибка API (Order): {response.status_code} - {error_detail}")
            await message.answer(f"❌ Ошибка на стороне сервера (Status {response.status_code}). Попробуйте позже.")
                
    except httpx.ConnectError:
        logger.error(f"Connection error to {settings.API_BASE_URL}")
        await message.answer("❌ Не удалось связаться с сервером. Убедитесь, что Бэкенд запущен.")
    except Exception as e:
        logger.exception(f"Критическая ошибка при заказе: {e}")
        await message.answer(f"❌ Произошла непредвиденная ошибка: {e}")

@router.callback_query(F.data.startswith("accept_"))
async def accept_order_callback(callback: CallbackQuery, state: FSMContext):
    # callback.data имеет вид "accept_{order_id}"
    order_id = int(callback.data.split("_")[-1])
    driver_telegram_id = callback.from_user.id
    
    try:
        response = await get_http_client().post(
            "/taxi/accept",
            json={
                "order_id": order_id,
                "driver_telegram_id": driver_telegram_id
            }
        )
        
        if response.status_code == 200:
            order = response.json()

            # Краткое подтверждение в чате, где нажата кнопка (группа водителей)
            await callback.answer("Вы приняли заказ! ✅")
            try:
                # Помечаем заказ в группе как принятый и убираем кнопки
                await callback.message.edit_text(
                    f"🚕 Заказ #{order_id} принят водителем {callback.from_user.full_name}.",
                    reply_markup=None,
                )
            except Exception as e:
                logger.exception(f"Error editing group message after accept for order {order_id}: {e}")

            # Отправляем карточку заказа в ЛИЧНЫЕ сообщения водителю
            try:
                card_text = (
                    f"🚕 **Вы приняли заказ #{order_id}**\n\n"
                    f"👤 Клиент: {order.get('client_phone', 'не указан')}\n"
                    f"📍 Откуда: {order['from_address']}\n"
                    f"🏁 Куда: {order['to_address']}"
                    + (f"\n💬 Примечание: {order.get('comment')}" if order.get('comment') else "\n💬 Примечание: Нет")
                )
                await callback.bot.send_message(
                    chat_id=driver_telegram_id,
                    text=card_text,
                    reply_markup=keyboards.get_driver_accept_keyboard(
                        order_id,
                        order["from_address"],
                    ),
                )
            except Exception as e:
                logger.exception(f"Error sending private order card to driver {driver_telegram_id}: {e}")
            
            # Notify client with real car details и кнопкой связи (только звонок на этапе accepted).
            # Защита от ситуации, когда по ошибке в БД хранится ID бота (bots can't send messages to bots).
            client_chat_id = order['client_telegram_id']
            driver_phone = order.get("driver_phone")
            try:
                me = await callback.bot.get_me()
                if client_chat_id == me.id:
                    logger.warning(
                        "Попытка отправить сообщение самому боту (client_telegram_id=%s). "
                        "Телеграм не позволяет ботам писать ботам.",
                        client_chat_id,
                    )
                else:
                    text = (
                        "🚕 **Водитель найден!**\n\n"
                        f"👤 Имя: {callback.from_user.full_name}\n"
                        f"🚗 Машина: {order['car_model']}\n"
                        f"🔢 Номер: {order['car_number']}\n\n"
                        "🚕 Водитель скоро прибудет."
                    )
                    sent_msg = await callback.bot.send_message(
                        client_chat_id,
                        text,
                        reply_markup=keyboards.get_client_after_accept_keyboard(order_id),
                    )

                    # 3) Сохраняем ID этого сообщения пассажиру — оно тоже часть "чека"
                    p_state = _get_passenger_state(callback.bot, state.storage, client_chat_id)
                    pdata = await p_state.get_data()
                    to_del = pdata.get("msg_to_delete", [])
                    to_del.append(sent_msg.message_id)
                    await p_state.update_data(msg_to_delete=to_del)
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
        logger.exception(f"Error in accept_order_callback: {e}")
        await callback.answer("❌ Ошибка при принятии заказа", show_alert=True)

@router.callback_query(F.data.startswith("complete_"))
async def complete_order_callback(callback: CallbackQuery, state: FSMContext):
    # callback.data имеет вид "complete_{order_id}"
    order_id = int(callback.data.split("_")[-1])
    
    try:
        # Сначала пробуем удалить само сообщение с кнопкой "Завершить", если оно ещё существует
        try:
            await callback.message.delete()
        except Exception as e:
            logger.exception(f"Error deleting driver 'complete' message for order {order_id}: {e}")

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
                except Exception as e:
                    logger.exception(f"Error deleting last_out_msg_id {out_msg_id}: {e}")
        except Exception as e:
            logger.exception(f"Error reading state in complete_order_callback: {e}")

        response = await get_http_client().post(f"/taxi/order/{order_id}/complete")
        
        if response.status_code == 200:
            data = response.json()
            client_chat_id = data.get("client_telegram_id")

            # "Чистый чек": Удаляем все временные сообщения пассажиру ПЕРЕД отправкой чека
            if client_chat_id is not None:
                try:
                    p_state = _get_passenger_state(callback.bot, state.storage, client_chat_id)
                    p_data = await p_state.get_data()
                    msg_to_delete = p_data.get("msg_to_delete") or []

                    if not msg_to_delete:
                        print(f"FATAL ERROR: msg_to_delete is EMPTY for passenger {client_chat_id}")
                    else:
                        print(f"DEBUG: Начинаю удаление для пассажира {client_chat_id}. Список ID: {msg_to_delete}")
                        # Удаляем все сообщения из списка
                        for mid in msg_to_delete:
                            try:
                                await callback.bot.delete_message(
                                    chat_id=client_chat_id,
                                    message_id=mid,
                                )
                                print(f"DEBUG: Удалено сообщение {mid}")
                            except Exception as e:
                                print(f"DEBUG: Ошибка удаления {mid}: {e}")

                    # Получаем данные заказа для красивого "чека"
                    order_resp = await get_http_client().get(f"/taxi/order/{order_id}")
                    ord_data = order_resp.json() if order_resp.status_code == 200 else {}
                    print(f"DEBUG: Данные заказа: {ord_data}")

                    if not ord_data:
                        logger.error(f"ORD_DATA пустой при завершении заказа {order_id}")

                    driver_name = ord_data.get("driver_name", "R S")
                    car_model = ord_data.get("car_model", "Гранта")
                    car_number = ord_data.get("car_number", "255")

                    # Формируем финальный чек в том же формате, что и сообщение «Водитель найден»
                    receipt_text = (
                        "✅ Поездка завершена!\n\n"
                        f"🚖 Ваш заказ выполнил: {driver_name}\n"
                        f"🚗 Машина: {car_model}\n"
                        f"🔢 Номер: {car_number}\n\n"
                        "🙏 Спасибо, что воспользовались нашим сервисом!\n"
                        "Пожалуйста, оцените работу водителя:"
                    )

                    # Отправляем "чистый чек" с инлайн-оценкой (звезды)
                    await callback.bot.send_message(
                        chat_id=client_chat_id,
                        text=receipt_text,
                        reply_markup=keyboards.get_rate_trip_keyboard(order_id),
                    )

                    # Сразу следом показываем inline‑кнопку "🚕 Заказать новое такси"
                    await callback.bot.send_message(
                        chat_id=client_chat_id,
                        text="🚕 Нажмите кнопку ниже, чтобы заказать новое такси:",
                        reply_markup=keyboards.get_new_order_after_rating_keyboard(),
                    )
                except Exception as e:
                    logger.exception(f"Error sending 'trip completed' (receipt) message to client {client_chat_id}: {e}")

            await callback.answer("Заказ завершен! Отличная работа 👍")
            # Удаляем кнопку "Завершить" у водителя и отправляем ему уведомление с номером заказа и суммой
            try:
                await callback.message.delete()
            except Exception:
                pass
            order_resp = await get_http_client().get(f"/taxi/order/{order_id}")
            ord_data = order_resp.json() if order_resp.status_code == 200 else {}
            price = ord_data.get("price")
            price_str = f"{price:.0f} руб." if price is not None else "—"
            await callback.bot.send_message(
                callback.from_user.id,
                f"✅ Заказ №{order_id} успешно завершен!\n💰 Сумма к оплате: {price_str}",
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

@router.callback_query(F.data.startswith("cancel_"))
async def cancel_order_callback(callback: CallbackQuery):
    # callback.data имеет вид "cancel_{order_id}"
    order_id = int(callback.data.split("_")[-1])
    
    try:
        response = await get_http_client().post(
            f"/taxi/order/{order_id}/cancel",
            params={"telegram_id": callback.from_user.id}
        )
        
        if response.status_code == 200:
            await callback.answer("Заказ отменен ❌")
            await callback.message.edit_text(
                f"❌ Заказ #{order_id} был отменен вами."
            )
        else:
            await callback.answer("Ошибка при отмене: заказ уже принят или не найден", show_alert=True)
    except Exception as e:
        logger.exception(f"Error in cancel_order_callback: {e}")
        await callback.answer("❌ Ошибка при отмене заказа", show_alert=True)
@router.callback_query(F.data.startswith("approve_"))
async def approve_driver_callback(callback: CallbackQuery):
    driver_id = int(callback.data.split("_")[1])
    try:
        response = await get_http_client().post(f"/taxi/driver/{driver_id}/approve")
        if response.status_code == 200:
            data = response.json()
            await callback.bot.send_message(
                data['telegram_id'],
                "✅ Ваша заявка одобрена! Теперь вы можете принимать заказы."
            )
            await callback.message.edit_text(callback.message.text + "\n\n✅ Одобрен")
        else:
            await callback.answer("Ошибка при одобрении", show_alert=True)
    except Exception as e:
        logger.exception(f"Error in approve_driver_callback: {e}")
        await callback.answer("❌ Ошибка при одобрении водителя", show_alert=True)

@router.callback_query(F.data.startswith("reject_"))
async def reject_driver_callback(callback: CallbackQuery):
    driver_id = int(callback.data.split("_")[1])
    try:
        response = await get_http_client().post(f"/taxi/driver/{driver_id}/reject")
        if response.status_code == 200:
            data = response.json()
            await callback.bot.send_message(
                data['telegram_id'],
                "❌ Ваша заявка на регистрацию водителем отклонена."
            )
            await callback.message.edit_text(callback.message.text + "\n\n❌ Отклонен")
        else:
            await callback.answer("Ошибка при отклонении", show_alert=True)
    except Exception as e:
        logger.exception(f"Error in reject_driver_callback: {e}")
        await callback.answer("❌ Ошибка при отклонении водителя", show_alert=True)

@router.callback_query(F.data.startswith("ignore_"))
async def ignore_order_callback(callback: CallbackQuery):
    # Just delete the message for this driver
    await callback.message.delete()
    await callback.answer("Заказ скрыт")

@router.callback_query(F.data.startswith("at_place_"))
async def at_place_callback(callback: CallbackQuery, state: FSMContext):
    # DEBUG‑лог, чтобы убедиться, что хендлер вообще срабатывает
    print(f"DEBUG: at_place_callback triggered, raw data={callback.data}")
    logger.debug(f"at_place_callback triggered, raw data={callback.data}")

    # callback.data имеет вид "at_place_{order_id}", поэтому берём последний элемент
    order_id = int(callback.data.split("_")[-1])
    try:
        # Обновляем статус заказа на стороне бэкенда и получаем данные клиента
        response = await get_http_client().post(f"/taxi/order/{order_id}/at_place")
        if response.status_code == 200:
            order = response.json()
            client_chat_id = order["client_telegram_id"]
            driver_phone = order.get("driver_phone")
            # Уведомляем клиента, с защитой от попытки писать боту
            try:
                me = await callback.bot.get_me()
                if client_chat_id == me.id:
                    logger.warning(
                        "Попытка отправить 'я на месте' самому боту (client_telegram_id=%s).",
                        client_chat_id,
                    )
                else:
                    # Сообщение без номера, номер отдаём через отдельную кнопку 'Позвонить'
                    text = (
                        "🚕 **Водитель на месте!**\n\n"
                        "Выходите, пожалуйста, машина ожидает вас по адресу."
                    )
                    sent = await callback.bot.send_message(
                        client_chat_id,
                        text,
                        reply_markup=keyboards.get_client_at_place_keyboard(order_id, driver_phone),
                    )
                    # 3) Добавить это сообщение в список msg_to_delete и сохранить ID для удаления при старте поездки
                    p_state = _get_passenger_state(callback.bot, state.storage, client_chat_id)
                    pdata = await p_state.get_data()
                    to_del = pdata.get("msg_to_delete", [])
                    to_del.append(sent.message_id)
                    await p_state.update_data(
                        msg_to_delete=to_del,
                        waiting_message_id=sent.message_id,
                    )
            except Exception as e:
                logger.exception(f"Error sending 'at place' message to client {client_chat_id}: {e}")

            # Сообщение водителю
            await callback.answer("Вы отметили, что на месте ✅")
            await callback.message.edit_text(
                f"🚕 Вы на месте по заказу #{order_id}.\n\n"
                "Ожидаем клиента. Нажмите 'Начать поездку', когда он сядет в машину.",
                reply_markup=keyboards.get_at_place_driver_keyboard(order_id),
            )
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
                        print(f"DEBUG: Сохраняем для удаления msg_id={sent.message_id} для водителя {driver_chat_id}")
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

            # Обновляем сообщение клиента и оставляем только кнопку связи
            try:
                mobj = await callback.message.edit_text(
                    "✅ Водитель уведомлен. Пожалуйста, выходите к машине. Приятного пути! 🚕",
                    reply_markup=keyboards.get_client_after_out_keyboard(order_id),
                )
                # 3) Добавить id этого сообщения пассажиру в список msg_to_delete
                p_state = _get_passenger_state(callback.bot, state.storage, callback.from_user.id)
                pdata = await p_state.get_data()
                to_del = pdata.get("msg_to_delete", [])
                to_del.append(mobj.message_id if hasattr(mobj, "message_id") else callback.message.message_id)
                await p_state.update_data(msg_to_delete=to_del)
            except Exception as e:
                logger.exception(f"Error editing client 'out' message: {e}")
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

            # Сообщение водителю и обновление интерфейса
            await callback.answer("Поездка отменена. Заказ снова доступен другим водителям.", show_alert=True)
            await callback.message.edit_text(
                f"❌ Вы отменили заказ #{order_id}. Заказ возвращён в общий список.",
            )
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
                except Exception as e:
                    logger.exception(f"Error deleting last_out_msg_id {out_msg_id} in start_trip_callback: {e}")
        except Exception as e:
            logger.exception(f"Error reading state in start_trip_callback: {e}")

        response = await get_http_client().post(f"/taxi/order/{order_id}/start")
        if response.status_code == 200:
            data = response.json()
            client_chat_id = data.get("client_telegram_id")
            to_address = data.get("to_address", "Адрес не указан")

            # Обновляем сообщение водителя
            await callback.message.edit_text(
                "🚕 ПОЕЗДКА НАЧАТА.\n\n"
                "Ожидайте указаний по маршруту и следуйте к точке назначения.",
                reply_markup=keyboards.get_in_progress_driver_keyboard(order_id, to_address),
            )
            await callback.answer("Поездка начата 🚕")

            # Уведомляем клиента: удаляем сообщение «Водитель на месте» (или убираем кнопки), затем отправляем новое
            if client_chat_id is not None:
                try:
                    p_state = _get_passenger_state(callback.bot, state.storage, client_chat_id)
                    p_data = await p_state.get_data()
                    waiting_message_id = p_data.get("waiting_message_id")
                    if isinstance(waiting_message_id, int):
                        try:
                            await callback.bot.delete_message(
                                chat_id=client_chat_id,
                                message_id=waiting_message_id,
                            )
                        except Exception as e:
                            try:
                                await callback.bot.edit_message_reply_markup(
                                    chat_id=client_chat_id,
                                    message_id=waiting_message_id,
                                    reply_markup=None,
                                )
                            except Exception as e2:
                                logger.warning(
                                    "Could not delete or edit waiting message %s for client %s: %s; %s",
                                    waiting_message_id, client_chat_id, e, e2,
                                )
                        await p_state.update_data(waiting_message_id=None)

                    sent = await callback.bot.send_message(
                        client_chat_id,
                        "🚕 Поездка началась! Желаем приятного пути.",
                    )
                    # Добавить новое сообщение пассажиру в список msg_to_delete
                    to_del = p_data.get("msg_to_delete", [])
                    to_del.append(sent.message_id)
                    await p_state.update_data(msg_to_delete=to_del)
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
    Клиент нажал '🚕 Заказать новое такси' после оценки.
    Сбрасываем состояние и переводим к вводу адреса 'откуда',
    не удаляя сообщение с оценкой из истории.
    """
    # Сбрасываем предыдущее состояние
    await state.clear()

    # Шаг 1: убираем кнопку "Заказать новое такси" у старого сообщения, текст с оценкой не трогаем
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception as e:
        logger.exception(f"Error editing reply markup in start_new_order_callback: {e}")

    # Шаг 2: отправляем новое сообщение для ввода адреса
    try:
        await callback.message.answer(
            "Отлично! Начинаем новый заказ. 🚕\n\n"
            "Напишите адрес, откуда вас забрать: 📍",
        )
    except Exception as e:
        logger.exception(f"Error sending new order prompt: {e}")
        await callback.answer("Произошла ошибка, попробуйте ещё раз.", show_alert=True)
        return

    # Шаг 3: переводим в состояние ожидания адреса 'откуда'
    await state.set_state(OrderTaxi.waiting_for_from_address)

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
        # Данные заказа для единого оформления (как в «Водитель найден» и в чеке)
        order_resp = await get_http_client().get(f"/taxi/order/{order_id}")
        ord_data = order_resp.json() if order_resp.status_code == 200 else {}
        driver_name = ord_data.get("driver_name", "R S")
        car_model = ord_data.get("car_model", "Гранта")
        car_number = ord_data.get("car_number", "255")

        new_text = (
            "✅ Поездка завершена!\n\n"
            f"🚖 Ваш заказ выполнил: {driver_name}\n"
            f"🚗 Машина: {car_model}\n"
            f"🔢 Номер: {car_number}\n\n"
            f"⭐ Ваша оценка: {rating_str} из 5\n"
            "🙏 Спасибо за отзыв! Теперь вы можете заказать новую машину."
        )

        await callback.message.edit_text(
            new_text,
            reply_markup=None,  # убираем звёзды, не дублируем кнопку заказа
        )
    except Exception as e:
        logger.exception(f"Error editing rating message: {e}")

    # Дополнительно уведомляем водителя об оценке
    try:
        # Берём свежие данные заказа только для того, чтобы узнать driver_telegram_id
        response = await get_http_client().get(f"/taxi/order/{order_id}")
        if response.status_code == 200:
            order = response.json()
            driver_chat_id = order.get("driver_telegram_id")
            if driver_chat_id:
                try:
                    me = await callback.bot.get_me()
                    if driver_chat_id == me.id:
                        logger.warning(
                            "Попытка отправить уведомление об оценке самому боту (driver_telegram_id=%s).",
                            driver_chat_id,
                        )
                    else:
                        rating_int = int(rating_str) if rating_str.isdigit() else None
                        extra = ""
                        if rating_int is not None and rating_int <= 2:
                            extra = "\n💬 Оценка влияет на ваш рейтинг. Старайтесь быть вежливее!"

                        await callback.bot.send_message(
                            chat_id=driver_chat_id,
                            text=(
                                f"⭐ Клиент поставил вам оценку {rating_str}/5 за заказ #{order_id}!"
                                f"{extra}"
                            ),
                        )
                except Exception as e:
                    logger.exception(f"Error sending rating notification to driver {driver_chat_id}: {e}")
    except Exception as e:
        logger.exception(f"Error fetching order for rating notification: {e}")

@router.message()
async def dbg_echo(message: Message, state: FSMContext):
    """
    Обработка прочих текстовых сообщений.
    - Если пользователь находится в процессе оформления заказа (есть активное состояние FSM),
      не засоряем чат лишними ответами.
    - Если состояние не активно, мягко подсказываем использовать главное меню.
    """
    data = await state.get_data()
    current_state = await state.get_state()

    # Если есть активное состояние (пользователь в процессе сценария) — просто игнорируем сообщение
    if current_state is not None:
        logger.debug(
            "Ignoring free-text message '%s' from %s in state %s",
            message.text,
            message.from_user.id,
            current_state,
        )
        return

    # Вне сценария: предлагаем воспользоваться главным меню
    await message.answer(
        "Чтобы вызвать машину, воспользуйтесь кнопкой '🚕 Заказать такси' в меню ниже👇",
        reply_markup=keyboards.get_main_menu(),
    )
