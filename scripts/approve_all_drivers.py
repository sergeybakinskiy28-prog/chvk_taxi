import asyncio
import os
import sys

# Добавляем путь к корню проекта
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sqlalchemy import update
from chvk_city.backend.database.db import engine
from chvk_city.backend.models.driver import Driver

async def approve_all():
    print("Approving all drivers for testing purposes...")
    async with engine.begin() as conn:
        await conn.execute(update(Driver).values(is_approved=True))
        print("All drivers have been approved!")

if __name__ == "__main__":
    asyncio.run(approve_all())
