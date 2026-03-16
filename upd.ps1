param($msg = "update")

# 1. Сохранение
git add .
git commit -m $msg
git push origin main

# 2. Запуск из правильной папки
Write-Host "--- STARTING BOT FROM BACKEND ---" -ForegroundColor Cyan

# Используем найденный путь
python chvk_city/backend/main.py