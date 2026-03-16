param($msg = "update")

# 1. Гит
git add .
git commit -m $msg
git push origin main

# 2. Запуск
Write-Host "--- STARTING BOT ---" -ForegroundColor Cyan
$env:PYTHONPATH = "."

# Запуск FastAPI бота так, чтобы он не выключался
Start-Process powershell -ArgumentList "uvicorn chvk_city.backend.main:app --host 0.0.0.0 --port 8000 --reload"