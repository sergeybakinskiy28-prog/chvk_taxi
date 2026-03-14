import asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from chvk_city.backend.config import settings
from chvk_city.backend.models.user import User
from chvk_city.backend.models.driver import Driver
from chvk_city.backend.models.order import Order

async def inspect():
    engine = create_async_engine(settings.DATABASE_URL)
    async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    
    async with async_session() as db:
        print("--- USERS ---")
        users = await db.execute(select(User))
        for u in users.scalars().all():
            print(f"ID: {u.id}, TG: {u.telegram_id}, Name: {u.name}, Phone: {u.phone}")
            
        print("\n--- DRIVERS ---")
        drivers = await db.execute(select(Driver))
        for d in drivers.scalars().all():
            print(f"ID: {d.id}, User ID: {d.user_id}, Model: {d.car_model}, Approved: {d.is_approved}")
            
        print("\n--- ORDERS ---")
        orders = await db.execute(select(Order))
        for o in orders.scalars().all():
            print(f"ID: {o.id}, User ID: {o.user_id}, Status: {o.status}, From: {o.from_address}")

if __name__ == "__main__":
    asyncio.run(inspect())
