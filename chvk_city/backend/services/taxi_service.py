from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select
from chvk_city.backend.models.order import Order
from chvk_city.backend.models.user import User
from chvk_city.backend.models.driver import Driver
from typing import List

class TaxiService:
    @staticmethod
    async def get_or_create_user(
        db: AsyncSession,
        telegram_id: int,
        name: str | None = None,
    ) -> User:
        result = await db.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        
        if not user:
            user = User(telegram_id=telegram_id, name=name)
            db.add(user)
            try:
                await db.commit()
                await db.refresh(user)
            except Exception:
                await db.rollback()
                raise
        else:
            if name is not None:
                user.name = name
                try:
                    await db.commit()
                    await db.refresh(user)
                except Exception:
                    await db.rollback()
                    raise
        return user

    @staticmethod
    async def create_order(db: AsyncSession, telegram_id: int, from_address: str, to_address: str, comment: str | None = None) -> Order:
        # Get user by telegram_id
        user = await TaxiService.get_or_create_user(db, telegram_id)
        
        new_order = Order(
            user_id=user.id,
            from_address=from_address,
            to_address=to_address,
            comment=comment,
            status="new"
        )
        db.add(new_order)
        try:
            await db.commit()
            await db.refresh(new_order)
        except Exception:
            await db.rollback()
            raise
        return new_order

    @staticmethod
    async def accept_order(db: AsyncSession, order_id: int, driver_telegram_id: int) -> Order | None:
        result = await db.execute(select(Order).where(Order.id == order_id))
        order = result.scalar_one_or_none()
        
        if order and order.status == "new":
            # Finding driver record by telegram_id through User table
            driver_result = await db.execute(
                select(Driver).join(User).where(User.telegram_id == driver_telegram_id)
            )
            driver = driver_result.scalar_one_or_none()
            
            if driver and driver.is_approved:
                order.driver_id = driver.id
                order.status = "accepted"
                try:
                    await db.commit()
                    await db.refresh(order)
                except Exception:
                    await db.rollback()
                    raise
                return order
        return None

    @staticmethod
    async def get_user_orders(db: AsyncSession, telegram_id: int) -> List[Order]:
        user_result = await db.execute(select(User).where(User.telegram_id == telegram_id))
        user = user_result.scalar_one_or_none()
        if not user:
            return []
            
        result = await db.execute(select(Order).where(Order.user_id == user.id).order_by(Order.created_at.desc()))
        return list(result.scalars().all())

    @staticmethod
    async def get_recent_completed_orders(
        db: AsyncSession,
        telegram_id: int,
        limit: int = 5,
    ) -> List[Order]:
        user_result = await db.execute(select(User).where(User.telegram_id == telegram_id))
        user = user_result.scalar_one_or_none()
        if not user:
            return []

        result = await db.execute(
            select(Order)
            .where(Order.user_id == user.id, Order.status == "completed")
            .order_by(Order.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    @staticmethod
    async def get_recent_unique_addresses(
        db: AsyncSession,
        telegram_id: int,
        address_type: str,
        limit: int = 3,
    ) -> List[str]:
        user_result = await db.execute(select(User).where(User.telegram_id == telegram_id))
        user = user_result.scalar_one_or_none()
        if not user:
            return []

        address_column = Order.from_address if address_type == "from" else Order.to_address

        result = await db.execute(
            select(
                address_column.label("address"),
                func.max(Order.created_at).label("last_used"),
            )
            .where(
                Order.user_id == user.id,
                Order.status == "completed",
                address_column.is_not(None),
                address_column != "",
            )
            .group_by(address_column)
            .order_by(func.max(Order.created_at).desc())
            .limit(limit)
        )

        return [row.address for row in result.all() if row.address]
    @staticmethod
    async def get_driver(db: AsyncSession, telegram_id: int) -> Driver | None:
        result = await db.execute(
            select(Driver).join(User).where(User.telegram_id == telegram_id)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def register_driver(db: AsyncSession, telegram_id: int, car_model: str, car_number: str) -> Driver:
        user = await TaxiService.get_or_create_user(db, telegram_id)
        
        driver = await TaxiService.get_driver(db, telegram_id)
        if not driver:
            driver = Driver(
                user_id=user.id,
                car_model=car_model,
                car_number=car_number
            )
            db.add(driver)
        else:
            driver.car_model = car_model
            driver.car_number = car_number
            
        try:
            await db.commit()
            await db.refresh(driver)
        except Exception:
            await db.rollback()
            raise
            
        return driver

    @staticmethod
    async def cancel_order(db: AsyncSession, order_id: int, telegram_id: int) -> bool:
        user_result = await db.execute(select(User).where(User.telegram_id == telegram_id))
        user = user_result.scalar_one_or_none()
        
        if not user:
            return False
            
        result = await db.execute(select(Order).where(Order.id == order_id, Order.user_id == user.id))
        order = result.scalar_one_or_none()
        
        if order and order.status == "new":
            order.status = "cancelled"
            try:
                await db.commit()
                return True
            except Exception:
                await db.rollback()
                raise
        return False

    @staticmethod
    async def complete_order(db: AsyncSession, order_id: int) -> bool:
        result = await db.execute(select(Order).where(Order.id == order_id))
        order = result.scalar_one_or_none()
        # Разрешаем завершать заказ из статусов 'accepted', 'at_place' и 'in_progress'
        if order and order.status in ("accepted", "at_place", "in_progress"):
            order.status = "completed"
            try:
                await db.commit()
                return True
            except Exception:
                await db.rollback()
                raise
        return False

    @staticmethod
    async def driver_cancel_order(db: AsyncSession, order_id: int, driver_telegram_id: int) -> bool:
        """
        Отмена заказа со стороны водителя.
        Логика:
        - Если заказ в статусе 'accepted' и закреплён за этим водителем,
          то освобождаем заказ для других водителей:
            - status -> 'new'
            - driver_id -> None
        - Если заказ уже не 'accepted' или водитель не совпадает — возвращаем False.
        """
        result = await db.execute(select(Order).where(Order.id == order_id))
        order = result.scalar_one_or_none()
        # Разрешаем отмену водителем только из активных состояний
        if not order or order.status not in ("accepted", "at_place"):
            return False

        # Находим водителя по telegram_id и убеждаемся, что именно он закреплён за заказом
        driver_result = await db.execute(
            select(Driver).join(User).where(User.telegram_id == driver_telegram_id)
        )
        driver = driver_result.scalar_one_or_none()
        if not driver or order.driver_id != driver.id:
            return False

        order.driver_id = None
        order.status = "new"

        try:
            await db.commit()
            return True
        except Exception:
            await db.rollback()
            raise

    @staticmethod
    async def update_user_phone(db: AsyncSession, telegram_id: int, phone: str) -> bool:
        result = await db.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if user:
            user.phone = phone
            try:
                await db.commit()
                return True
            except Exception:
                await db.rollback()
                raise
        return False

    @staticmethod
    async def approve_driver(db: AsyncSession, driver_id: int) -> bool:
        result = await db.execute(select(Driver).where(Driver.id == driver_id))
        driver = result.scalar_one_or_none()
        if driver:
            driver.is_approved = True
            try:
                await db.commit()
                return True
            except Exception:
                await db.rollback()
                raise
        return False

    @staticmethod
    async def reject_driver(db: AsyncSession, driver_id: int) -> bool:
        result = await db.execute(select(Driver).where(Driver.id == driver_id))
        driver = result.scalar_one_or_none()
        if driver:
            await db.delete(driver)
            try:
                await db.commit()
                return True
            except Exception:
                await db.rollback()
                raise
        return False
