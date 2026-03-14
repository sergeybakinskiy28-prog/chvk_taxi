FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=off \
    PIP_DISABLE_PIP_VERSION_CHECK=on \
    PIP_DEFAULT_TIMEOUT=100

WORKDIR /app

# Устанавливаем системные зависимости (для сборки зависимостей, работы с БД и т.п.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Устанавливаем Python-зависимости
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && pip install -r requirements.txt

# Копируем проект
COPY . /app

# По умолчанию запускаем API, команду можно переопределить в docker-compose
CMD ["uvicorn", "chvk_city.backend.main:app", "--host", "0.0.0.0", "--port", "8000"]

