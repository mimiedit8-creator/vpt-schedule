FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    STATE_FILE=/data/state.json

WORKDIR /app
COPY bot.py /app/bot.py
RUN mkdir -p /data && useradd --create-home --uid 10001 botuser && chown -R botuser:botuser /app /data
USER botuser

CMD ["python", "bot.py", "run"]
