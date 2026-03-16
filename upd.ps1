param($msg = "update")

# 1. Сохраняем и отправляем изменения
Write-Host "--- Сохраняю код ---" -ForegroundColor Yellow
git add .
git commit -m $msg
git push origin main

# 2. Запуск бота
Write-Host "--- Запускаю бота ---" -ForegroundColor Cyan

# Попробуем запустить через путь, который виден в твоем проекте
if (Test-Path "chvk_city/bot/telegram/main.py") {
    python chvk_city/bot/telegram/main.py
}
else {
    python main.py
}