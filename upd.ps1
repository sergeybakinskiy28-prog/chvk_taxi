param($msg = "update")

# 1. Сохранение
git add .
git commit -m $msg
git push origin main

# 2. Настройка путей и запуск
Write-Host "--- STARTING BOT ---" -ForegroundColor Cyan

# Указываем Python, что корень проекта здесь
$env:PYTHONPATH = "."

# Запускаем как модуль (через точку), это решит проблему с импортами
python chvk_city/backend/main.py