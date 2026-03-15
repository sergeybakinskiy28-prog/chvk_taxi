# Развёртывание CHVK City Taxi на VPS (Docker)

## Подготовка на сервере

### 1. Установите Docker и Docker Compose

```bash
# Ubuntu/Debian
sudo apt update
sudo apt install -y docker.io docker-compose-plugin
sudo usermod -aG docker $USER
# Перелогиньтесь или: newgrp docker
```

### 2. Клонируйте проект и перейдите в папку

```bash
git clone <URL_вашего_репозитория> taxi_project
cd taxi_project
```

### 3. Создайте файл `.env`

```bash
cp .env.example .env
nano .env   # или vim, замените значения на реальные
```

Обязательно заполните:
- `TELEGRAM_BOT_TOKEN` — токен от @BotFather
- `DRIVER_CHAT_ID` — ID чата водителей (группа)
- `ADMIN_CHAT_ID` — ID админ-чата
- `SECRET_KEY` — случайная строка для production
- `POSTGRES_PASSWORD` — пароль БД (если хотите изменить дефолтный)

### 4. Запустите все сервисы

```bash
docker compose up -d --build
```

Проверка логов:
```bash
docker compose logs -f
```

### 5. Остановка

```bash
docker compose down
```

С сохранением данных БД:
```bash
docker compose down   # volume postgres_data сохраняется
```

Полная очистка (включая БД):
```bash
docker compose down -v
```

---

## Сервисы

| Сервис   | Порт  | Описание                    |
|----------|-------|-----------------------------|
| db       | 5432  | PostgreSQL                  |
| api      | 8001  | FastAPI (внешний порт)      |
| bot      | —     | Telegram-бот               |

Бот обращается к API по адресу `http://api:8000` внутри Docker-сети. Снаружи API доступен на порту 8001.
