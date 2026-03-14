import asyncio
import os
import sys

# Добавляем путь к корню проекта, чтобы импорты работали
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sqlalchemy import text
from chvk_city.backend.database.db import engine

async def fix_database():
    print("Checking and fixing database schema...")
    async with engine.begin() as conn:
        # Добавляем is_approved, если ее нет
        await conn.execute(text("ALTER TABLE drivers ADD COLUMN IF NOT EXISTS is_approved BOOLEAN DEFAULT FALSE"))
        print("- Column 'is_approved' added or already exists.")
        
        # Добавляем rating, если ее нет (на всякий случай)
        await conn.execute(text("ALTER TABLE drivers ADD COLUMN IF NOT EXISTS rating FLOAT DEFAULT 5.0"))
        print("- Column 'rating' added or already exists.")
        
    print("Database fix completed!")

if __name__ == "__main__":
    asyncio.run(fix_database())
