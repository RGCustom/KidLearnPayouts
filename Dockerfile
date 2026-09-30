FROM python:3.12-alpine

LABEL org.opencontainers.image.source="https://github.com/RGCustom/KidLearnPayouts" \
      org.opencontainers.image.description="Учёт по Договору № 1/2026: накопления, выплаты, статистика"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CONFIG_DIR=/config \
    PORT=8080 \
    TZ=Europe/Moscow

RUN apk add --no-cache tzdata su-exec

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py entrypoint.sh ./
COPY templates ./templates
COPY static ./static
# на случай, если файл был сохранён на Windows с CRLF
RUN sed -i 's/\r$//' entrypoint.sh && chmod +x entrypoint.sh

VOLUME /config
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/healthz',timeout=4)" || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
