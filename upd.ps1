param($msg = "update")

# 1. Гит
git add .
git commit -m $msg
git push origin main

# 2. Запуск
# Если у тебя основной файл лежит в папке бота, укажи путь к нему.
# Например, если файл называется bot.py и лежит в корне:
python bot.py