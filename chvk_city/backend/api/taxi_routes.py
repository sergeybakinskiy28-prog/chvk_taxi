from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from chvk_city.backend.database.db import get_db
from chvk_city.backend.services.taxi_service import TaxiService
from chvk_city.backend.models.user import User
from chvk_city.backend.models.driver import Driver
from chvk_city.backend.models.order import Order
from sqlalchemy import select
from pydantic import BaseModel
from typing import List

router = APIRouter(prefix="/taxi", tags=["taxi"])

class OrderCreate(BaseModel):
    telegram_id: int
    from_address: str
    to_address: str
    comment: str | None = None

class OrderAccept(BaseModel):
    order_id: int
    driver_telegram_id: int

class UserRegister(BaseModel):
    telegram_id: int
    name: str | None = None

class DriverRegister(BaseModel):
    telegram_id: int
    car_model: str
    car_number: str

class UserUpdatePhone(BaseModel):
    telegram_id: int
    phone: str


class DriverCancelOrder(BaseModel):
    order_id: int
    driver_telegram_id: int


class OrderHistoryItem(BaseModel):
    id: int
    created_at: str
    from_address: str
    to_address: str
    price: float | None


class DriverInfo(BaseModel):
    id: int
    telegram_id: int
    is_approved: bool
    car_model: str
    car_number: str


class DriverDistrictUpdate(BaseModel):
    telegram_id: int
    district: str


class DriverListItem(BaseModel):
    id: int
    name: str | None
    car_model: str
    car_number: str
    current_district: str | None
    telegram_id: int | None = None

@router.post("/user/update_phone")
async def update_user_phone(data: UserUpdatePhone, db: AsyncSession = Depends(get_db)):
    success = await TaxiService.update_user_phone(db, data.telegram_id, data.phone)
    if not success:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    return {"status": "success"}

@router.post("/user/register")
async def register_user(user_data: UserRegister, db: AsyncSession = Depends(get_db)):
    user = await TaxiService.get_or_create_user(db, user_data.telegram_id, user_data.name)
    return {"id": user.id, "telegram_id": user.telegram_id, "name": user.name}


@router.get("/user/{telegram_id}")
async def get_user(telegram_id: int, db: AsyncSession = Depends(get_db)):
    """
    Получение пользователя по telegram_id для проверки, есть ли уже телефон.
    """
    result = await db.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    return {
        "id": user.id,
        "telegram_id": user.telegram_id,
        "name": user.name,
        "phone": user.phone,
    }


@router.get("/driver/by_telegram/{telegram_id}", response_model=DriverInfo)
async def get_driver_by_telegram(telegram_id: int, db: AsyncSession = Depends(get_db)):
    """
    Возвращает информацию о водителе по его telegram_id.
    Используется ботом для проверки, зарегистрирован ли пользователь как водитель.
    """
    result = await db.execute(
        select(Driver, User).join(User).where(User.telegram_id == telegram_id)
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Водитель не найден")

    driver, user = row
    return DriverInfo(
        id=driver.id,
        telegram_id=user.telegram_id,
        is_approved=driver.is_approved,
        car_model=driver.car_model,
        car_number=driver.car_number,
    )


@router.post("/driver/set_district")
async def set_driver_district(data: DriverDistrictUpdate, db: AsyncSession = Depends(get_db)):
    """
    Сохраняет выбранный водителем район в поле current_district модели Driver.
    """
    result = await db.execute(
        select(Driver, User).join(User).where(User.telegram_id == data.telegram_id)
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Водитель не найден")

    driver, user = row

    driver.current_district = data.district

    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    return {
        "status": "success",
        "message": f"Вы встали в очередь: {data.district}",
    }


@router.get("/admin/drivers/pending", response_model=list[DriverListItem])
async def get_pending_drivers(db: AsyncSession = Depends(get_db)):
    """
    Список водителей с is_approved=False (новые заявки на одобрение).
    """
    result = await db.execute(
        select(Driver, User)
        .join(User, Driver.user_id == User.id)
        .where(Driver.is_approved.is_(False))
    )
    rows = result.all()
    items: list[DriverListItem] = []
    for driver, user in rows:
        items.append(
            DriverListItem(
                id=driver.id,
                name=user.name,
                car_model=driver.car_model,
                car_number=driver.car_number,
                current_district=driver.current_district,
                telegram_id=user.telegram_id,
            )
        )
    return items


@router.get("/drivers/all", response_model=list[DriverListItem])
async def get_all_drivers(db: AsyncSession = Depends(get_db)):
    """
    Список всех ОДОБРЕННЫХ водителей для панели администратора.
    """
    result = await db.execute(
        select(Driver, User)
        .join(User, Driver.user_id == User.id)
        .where(Driver.is_approved.is_(True))
    )
    rows = result.all()
    items: list[DriverListItem] = []
    for driver, user in rows:
        items.append(
            DriverListItem(
                id=driver.id,
                name=user.name,
                car_model=driver.car_model,
                car_number=driver.car_number,
                current_district=driver.current_district,
                telegram_id=user.telegram_id,
            )
        )
    return items


@router.get("/admin/driver/{tg_id}/confirm_info")
async def get_driver_confirm_info(tg_id: int, db: AsyncSession = Depends(get_db)):
    """
    Информация о водителе для подтверждения удаления: имя, username, авто.
    """
    result = await db.execute(
        select(Driver, User)
        .join(User, Driver.user_id == User.id)
        .where(User.telegram_id == tg_id)
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Водитель не найден")

    driver, user = row
    return {
        "telegram_id": user.telegram_id,
        "name": user.name or "—",
        "car_model": driver.car_model,
        "car_number": driver.car_number,
    }


@router.delete("/admin/driver/{tg_id}")
async def admin_delete_driver(tg_id: int, db: AsyncSession = Depends(get_db)):
    """
    Удаление/увольнение водителя по telegram_id для владельца/админа.
    """
    result = await db.execute(
        select(Driver, User).join(User, Driver.user_id == User.id).where(User.telegram_id == tg_id)
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Водитель не найден")

    driver, user = row
    success = await TaxiService.reject_driver(db, driver.id)
    if not success:
        raise HTTPException(status_code=400, detail="Не удалось удалить водителя")

    return {"status": "deleted", "telegram_id": user.telegram_id}

@router.post("/driver/register")
async def register_driver(driver_data: DriverRegister, db: AsyncSession = Depends(get_db)):
    driver = await TaxiService.register_driver(
        db, driver_data.telegram_id, driver_data.car_model, driver_data.car_number
    )
    # Get driver telegram_id for bot convenience
    user_result = await db.execute(select(User).where(User.id == driver.user_id))
    user = user_result.scalar_one()
    
    return {
        "status": "success", 
        "driver_id": driver.id,
        "telegram_id": user.telegram_id,
        "car_model": driver.car_model,
        "car_number": driver.car_number
    }

@router.post("/driver/{driver_id}/approve")
async def approve_driver(driver_id: int, db: AsyncSession = Depends(get_db)):
    success = await TaxiService.approve_driver(db, driver_id)
    if not success:
        raise HTTPException(status_code=404, detail="Водитель не найден")
    
    # Fetch driver and user to get telegram_id for notification
    driver_result = await db.execute(select(Driver, User).join(User).where(Driver.id == driver_id))
    found = driver_result.one_or_none()
    if not found:
        raise HTTPException(status_code=404, detail="Водитель или пользователь не найден")
    
    driver, user = found
    
    return {"status": "approved", "telegram_id": user.telegram_id}

@router.post("/driver/{driver_id}/reject")
async def reject_driver(driver_id: int, db: AsyncSession = Depends(get_db)):
    # Get telegram_id before deletion
    driver_result = await db.execute(select(Driver, User).join(User).where(Driver.id == driver_id))
    found = driver_result.one_or_none()
    if not found:
        raise HTTPException(status_code=404, detail="Водитель не найден")
    
    driver, user = found
    tg_id = user.telegram_id
    
    success = await TaxiService.reject_driver(db, driver_id)
    return {"status": "rejected", "telegram_id": tg_id}

@router.post("/order/{order_id}/complete")
async def complete_order(order_id: int, db: AsyncSession = Depends(get_db)):
    success = await TaxiService.complete_order(db, order_id)
    if not success:
        raise HTTPException(status_code=400, detail="Не удалось завершить заказ")

    result = await db.execute(
        select(Order, User)
        .join(User, Order.user_id == User.id)
        .where(Order.id == order_id)
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Заказ не найден после завершения")

    order, client_user = row
    out = {
        "status": "completed",
        "client_telegram_id": client_user.telegram_id,
        "from_address": order.from_address,
        "to_address": order.to_address,
        "price": order.price,
    }
    if order.driver_id is not None:
        dr_result = await db.execute(
            select(Driver, User)
            .join(User, Driver.user_id == User.id)
            .where(Driver.id == order.driver_id)
        )
        dr_row = dr_result.one_or_none()
        if dr_row:
            driver, driver_user = dr_row
            out["driver_name"] = driver_user.name or "R S"
            out["car_model"] = driver.car_model
            out["car_number"] = driver.car_number
    if "driver_name" not in out:
        out["driver_name"] = "R S"
        out["car_model"] = "Гранта"
        out["car_number"] = "255"
    return out


@router.post("/order/{order_id}/at_place")
async def order_at_place(order_id: int, db: AsyncSession = Depends(get_db)):
    """
    Отметка водителя 'я на месте'.
    Переводит заказ в статус 'at_place' и возвращает данные клиента для уведомлений.
    """
    result = await db.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    if order.status != "accepted":
        raise HTTPException(status_code=400, detail="Заказ не может быть переведён в статус 'на месте'")

    order.status = "at_place"
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    # Клиент
    user_result = await db.execute(select(User).where(User.id == order.user_id))
    client = user_result.scalar_one()

    # Водитель (для телефона и telegram_id)
    driver_telegram_id: int | None = None
    driver_phone: str | None = None
    if order.driver_id is not None:
        driver_result = await db.execute(
            select(Driver, User).join(User, Driver.user_id == User.id).where(Driver.id == order.driver_id)
        )
        driver_row = driver_result.one_or_none()
        if driver_row:
            driver, driver_user = driver_row
            driver_telegram_id = driver_user.telegram_id
            driver_phone = driver_user.phone

    return {
        "id": order.id,
        "client_telegram_id": client.telegram_id,
        "driver_telegram_id": driver_telegram_id,
        "driver_phone": driver_phone,
        "status": order.status,
        "from_address": order.from_address,
        "to_address": order.to_address,
    }


@router.post("/order/{order_id}/start")
async def start_order(order_id: int, db: AsyncSession = Depends(get_db)):
    """
    Начало поездки водителем.
    Переводит заказ из 'at_place' в 'in_progress' и возвращает данные для уведомлений.
    """
    result = await db.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    if order.status != "at_place":
        raise HTTPException(status_code=400, detail="Заказ не может быть переведён в статус 'в пути'")

    order.status = "in_progress"
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    # Клиент
    user_result = await db.execute(select(User).where(User.id == order.user_id))
    client = user_result.scalar_one()

    # Водитель
    driver_telegram_id: int | None = None
    if order.driver_id is not None:
        driver_result = await db.execute(
            select(Driver, User).join(User, Driver.user_id == User.id).where(Driver.id == order.driver_id)
        )
        driver_row = driver_result.one_or_none()
        if driver_row:
            driver, driver_user = driver_row
            driver_telegram_id = driver_user.telegram_id

    return {
        "id": order.id,
        "client_telegram_id": client.telegram_id,
        "driver_telegram_id": driver_telegram_id,
        "status": order.status,
        "from_address": order.from_address,
        "to_address": order.to_address,
    }

@router.post("/order/{order_id}/cancel")
async def cancel_order(order_id: int, telegram_id: int, db: AsyncSession = Depends(get_db)):
    success = await TaxiService.cancel_order(db, order_id, telegram_id)
    if not success:
        raise HTTPException(
            status_code=400, 
            detail="Не удалось отменить заказ (возможно, он уже принят или выполнен)"
        )
    return {"status": "cancelled"}


@router.post("/order/driver_cancel")
async def driver_cancel_order(data: DriverCancelOrder, db: AsyncSession = Depends(get_db)):
    """
    Отмена заказа со стороны водителя.
    Возвращает telegram_id клиента для уведомления.
    """
    success = await TaxiService.driver_cancel_order(db, data.order_id, data.driver_telegram_id)
    if not success:
        raise HTTPException(
            status_code=400,
            detail="Не удалось отменить заказ: он уже не активен или закреплён за другим водителем",
        )

    # Получаем данные заказа и клиента для уведомления
    result = await db.execute(select(Order).where(Order.id == data.order_id))
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Заказ не найден после отмены")

    user_result = await db.execute(select(User).where(User.id == order.user_id))
    user = user_result.scalar_one()

    return {
        "status": "driver_cancelled",
        "client_telegram_id": user.telegram_id,
        "from_address": order.from_address,
        "to_address": order.to_address,
    }

@router.post("/order")
async def create_order(order_data: OrderCreate, db: AsyncSession = Depends(get_db)):
    order = await TaxiService.create_order(
        db, order_data.telegram_id, order_data.from_address, order_data.to_address, order_data.comment
    )
    # Fetch user to get telegram_id for the response
    user_result = await db.execute(select(User).where(User.id == order.user_id))
    user = user_result.scalar_one()
    
    return {
        "id": order.id,
        "telegram_id": user.telegram_id, # Return telegram_id for bot convenience
        "from_address": order.from_address,
        "to_address": order.to_address,
        "comment": order.comment,
        "status": order.status
    }

@router.post("/accept")
async def accept_order(accept_data: OrderAccept, db: AsyncSession = Depends(get_db)):
    order = await TaxiService.accept_order(db, accept_data.order_id, accept_data.driver_telegram_id)
    if not order:
        # Дополнительная диагностика причин отказа
        # 1. Нет такого заказа
        order_result = await db.execute(select(Order).where(Order.id == accept_data.order_id))
        existing_order = order_result.scalar_one_or_none()
        if not existing_order:
            raise HTTPException(status_code=404, detail="Заказ не существует")

        # 2. Заказ уже не в статусе 'new' (уже принят/завершён/отменён)
        if existing_order.status != "new":
            raise HTTPException(status_code=409, detail="Заказ уже принят или завершён")

        # 3. Проверим водителя и его одобрение
        driver_check_result = await db.execute(
            select(Driver).join(User).where(User.telegram_id == accept_data.driver_telegram_id)
        )
        driver = driver_check_result.scalar_one_or_none()
        if not driver:
            raise HTTPException(status_code=404, detail="Водитель не найден")
        if not driver.is_approved:
            raise HTTPException(status_code=403, detail="Водитель не одобрен администратором")

        # Если мы сюда попали, значит логика в сервисе не отработала по неизвестной причине
        raise HTTPException(status_code=400, detail="Не удалось принять заказ по неизвестной причине")

    # Fetch client telegram_id for notification
    client_result = await db.execute(select(User).where(User.id == order.user_id))
    client = client_result.scalar_one()
    
    # Fetch driver and their user details
    driver_result = await db.execute(
        select(Driver, User).join(User, Driver.user_id == User.id).where(Driver.id == order.driver_id)
    )
    driver_row = driver_result.one_or_none()
    if not driver_row:
         raise HTTPException(status_code=404, detail="Информация о водителе не найдена")
    
    driver, driver_user = driver_row
    
    return {
        "id": order.id,
        "client_telegram_id": client.telegram_id,
        "client_phone": client.phone,          # телефон клиента
        "driver_phone": driver_user.phone,     # телефон водителя для клиента
        "status": order.status,
        "driver_name": driver_user.name or "", # Driver name from User model
        "car_model": driver.car_model,
        "car_number": driver.car_number,
        "from_address": order.from_address,    # Return addresses for driver UI convenience
        "to_address": order.to_address,
        "comment": order.comment,
    }

@router.get("/order/{order_id}")
async def get_order(order_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    
    user_result = await db.execute(select(User).where(User.id == order.user_id))
    user = user_result.scalar_one()

    # Водитель и его пользователь: telegram_id, телефон, имя, авто
    driver_telegram_id: int | None = None
    driver_phone: str | None = None
    driver_name: str = ""
    car_model: str = ""
    car_number: str = ""
    if order.driver_id is not None:
        driver_result = await db.execute(
            select(Driver, User).join(User, Driver.user_id == User.id).where(Driver.id == order.driver_id)
        )
        driver_row = driver_result.one_or_none()
        if driver_row:
            driver, driver_user = driver_row
            driver_telegram_id = driver_user.telegram_id
            driver_phone = driver_user.phone
            driver_name = driver_user.name or ""
            car_model = driver.car_model
            car_number = driver.car_number

    return {
        "id": order.id,
        "client_telegram_id": user.telegram_id,
        "driver_telegram_id": driver_telegram_id,
        "driver_phone": driver_phone,
        "driver_name": driver_name,
        "car_model": car_model,
        "car_number": car_number,
        "from_address": order.from_address,
        "to_address": order.to_address,
        "status": order.status,
        "comment": order.comment,
        "price": order.price,
    }

@router.get("/user/{telegram_id}/orders")
async def get_user_orders(telegram_id: int, db: AsyncSession = Depends(get_db)):
    orders = await TaxiService.get_user_orders(db, telegram_id)
    return orders


@router.get("/orders/history/{user_id}", response_model=List[OrderHistoryItem])
async def get_order_history(user_id: int, limit: int = 10, db: AsyncSession = Depends(get_db)):
    """
    История последних завершенных заказов пользователя.
    Возвращает не более `limit` записей со статусом 'completed',
    отсортированных по дате создания (новые сверху).
    """
    stmt = (
        select(Order)
        .where(Order.user_id == user_id, Order.status == "completed")
        .order_by(Order.created_at.desc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    orders = result.scalars().all()

    return [
        OrderHistoryItem(
            id=o.id,
            created_at=o.created_at.isoformat() if o.created_at else "",
            from_address=o.from_address,
            to_address=o.to_address,
            price=o.price,
        )
        for o in orders
    ]
