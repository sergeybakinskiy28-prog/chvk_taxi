param($msg = "update")

# 1. Гит
git add .
git commit -m $msg
git push origin main

# 2. Запуск
Write-Host "--- STARTING BOT ---" -ForegroundColor Cyan
$env:PYTHONPATH = "."

# Запускаем так, чтобы процесс не обрывался
python chvk_city/backend/main.py