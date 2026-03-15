-- Миграция: добавление колонки username в таблицу users
-- Запустите этот скрипт, если у вас уже есть БД без этой колонки:
-- psql -U postgres -d your_db -f add_username_column.sql

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns 
    WHERE table_schema = 'public' AND table_name = 'users' AND column_name = 'username'
  ) THEN
    ALTER TABLE users ADD COLUMN username VARCHAR(100) NULL;
  END IF;
END $$;
