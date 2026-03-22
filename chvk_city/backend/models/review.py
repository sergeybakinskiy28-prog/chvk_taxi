from sqlalchemy import Column, Integer, BigInteger, String, DateTime, func
from chvk_city.backend.database.db import Base


class Review(Base):
    __tablename__ = "reviews"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, nullable=False)
    driver_id = Column(Integer, nullable=False)
    client_telegram_id = Column(BigInteger, nullable=False)
    rating = Column(Integer, nullable=False)
    comment = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
