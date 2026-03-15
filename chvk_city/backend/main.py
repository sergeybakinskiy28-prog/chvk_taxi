from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
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

@app.get("/")
async def root():
    return {"message": "CHVK Taxi API is running"}
