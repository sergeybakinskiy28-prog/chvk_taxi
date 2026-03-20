from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from chvk_city.backend.api import taxi_routes
from chvk_city.backend.database.db import engine, Base

app = FastAPI(title="CHVK City Services - Taxi MVP")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(taxi_routes.router)

@app.on_event("startup")
async def startup():
    # In a real MVP with migrations we'd use Alembic,
    # but for quick launch we can create tables here if they don't exist.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Migration: add balance column to drivers if missing
        try:
            await conn.execute(
                text("ALTER TABLE drivers ADD COLUMN IF NOT EXISTS balance FLOAT DEFAULT 0.0")
            )
        except Exception:
            pass
        # Migration: add scheduled_at column to orders if missing
        try:
            await conn.execute(
                text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS scheduled_at TIMESTAMP")
            )
        except Exception:
            pass

@app.get("/")
async def root():
    return {"message": "CHVK Taxi API is running"}
