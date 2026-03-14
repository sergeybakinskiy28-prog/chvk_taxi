import asyncio
import logging
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from chvk_city.backend.config import settings
from chvk_city.backend.database.db import Base
# Import all models to ensure they are registered in Base.metadata
from chvk_city.backend.models.user import User
from chvk_city.backend.models.driver import Driver
from chvk_city.backend.models.order import Order

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("fix_db")

async def fix_schema():
    engine = create_async_engine(settings.DATABASE_URL)
    
    async def add_column_if_not_exists(table, column, type_def):
        async with engine.begin() as conn:
            try:
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {type_def}"))
                logger.info(f"Added column '{column}' to '{table}' table.")
            except Exception as e:
                if "already exists" in str(e).lower():
                    logger.info(f"Column '{column}' already exists in '{table}'.")
                else:
                    logger.error(f"Error adding '{column}' to '{table}': {e}")

    logger.info("Checking for missing columns...")
    
    await add_column_if_not_exists("orders", "comment", "VARCHAR(500)")
    await add_column_if_not_exists("orders", "price", "FLOAT")
    await add_column_if_not_exists("users", "phone", "VARCHAR(20)")

    logger.info("Schema fix completed!")

if __name__ == "__main__":
    asyncio.run(fix_schema())
