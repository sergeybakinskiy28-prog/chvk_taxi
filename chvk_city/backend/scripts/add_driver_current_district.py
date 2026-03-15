"""
Скрипт миграции: добавляет колонку current_district в таблицу drivers.
Запуск из корня проекта: python -m chvk_city.backend.scripts.add_driver_current_district
"""
import asyncio

from sqlalchemy import text
from chvk_city.backend.database.db import engine
from chvk_city.backend.config import settings


async def add_column():
    url = settings.DATABASE_URL
    if "sqlite" in url.lower():
        sql = "ALTER TABLE drivers ADD COLUMN current_district TEXT"
    else:
        # PostgreSQL и др.
        sql = "ALTER TABLE drivers ADD COLUMN IF NOT EXISTS current_district VARCHAR(50)"

    async with engine.begin() as conn:
        try:
            await conn.execute(text(sql))
            print("OK: колонка current_district добавлена в таблицу drivers.")
        except Exception as e:
            if "duplicate column name" in str(e).lower() or "already exists" in str(e).lower():
                print("Колонка current_district уже есть в таблице drivers.")
            else:
                raise


if __name__ == "__main__":
    asyncio.run(add_column())
