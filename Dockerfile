# Используем легкий образ Python 3.10
FROM python:3.10-slim

# Устанавливаем рабочую директорию внутри контейнера
WORKDIR /app

# Копируем файл зависимостей и устанавливаем их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь остальной код проекта
COPY . .

# Команда запуска (она запустит твой main.py, который поднимет и бота, и сервер)
CMD ["python", "main.py"]
