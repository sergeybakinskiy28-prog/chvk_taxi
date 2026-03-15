from sqlalchemy import BigInteger, String, Float, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column
from chvk_city.backend.database.db import Base
from datetime import datetime

class Driver(Base):
    __tablename__ = "drivers"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    car_model: Mapped[str] = mapped_column(String(255))
    car_number: Mapped[str] = mapped_column(String(20))
    rating: Mapped[float] = mapped_column(Float, default=5.0)
    is_approved: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # Текущий район/стоянка водителя (для логики смен)
    current_district: Mapped[str | None] = mapped_column(String(50), nullable=True)

class DriverStatus(Base):
    __tablename__ = "driver_statuses"

    driver_id: Mapped[int] = mapped_column(ForeignKey("drivers.id"), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="offline") # online, offline, busy
    location_lat: Mapped[float | None] = mapped_column(Float)
    location_lon: Mapped[float | None] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    area: Mapped[str | None] = mapped_column(String(50), nullable=True)
