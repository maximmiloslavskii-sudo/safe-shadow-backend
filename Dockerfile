FROM python:3.12-slim

# Системные зависимости для shapely/numpy
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libgeos-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Зависимости отдельным слоем — кэшируются при неизменных requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Код приложения
COPY app/ ./app/

# Порт по умолчанию (переопределяется через ENV PORT на Railway/Render)
ENV PORT=8000

EXPOSE $PORT

# Используем $PORT для совместимости с Railway/Render/Fly.io
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
