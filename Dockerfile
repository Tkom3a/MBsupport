FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Moscow

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shotcore ./shotcore
COPY config.yaml .

RUN mkdir -p /app/data /app/logs
EXPOSE 4861

CMD ["python", "-m", "shotcore", "--config", "/app/config.yaml"]
