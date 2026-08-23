FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=Europe/Moscow

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shotcore ./shotcore
COPY mbauth ./mbauth
COPY shotcli ./shotcli
COPY config.yaml .

RUN mkdir -p /app/data /app/logs
EXPOSE 4861

CMD ["python", "-m", "shotcore", "--config", "/app/config.yaml"]
