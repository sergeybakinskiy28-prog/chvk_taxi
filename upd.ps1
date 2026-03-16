param($msg = "update")

# 1. Git operations
git add .
git commit -m $msg
git push origin main

# 2. Start Bot
Write-Host "--- STARTING BOT ---" -ForegroundColor Cyan

# Пробуем запустить напрямую через путь к файлу
python chvk_city/bot/telegram/main.py