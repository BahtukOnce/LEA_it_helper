FROM python:3.12-slim

# чтобы не было проблем с выводом
ENV PYTHONUNBUFFERED=1

# рабочая директория внутри контейнера
WORKDIR /app

# сначала зависимости (кэшируется)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# потом копируем остальной код
COPY . .

# что запускать при старте контейнера
CMD ["python", "bot.py"]
