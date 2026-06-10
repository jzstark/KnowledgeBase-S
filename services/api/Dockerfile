FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Runs `alembic upgrade head` first when RUN_MIGRATIONS=1 (api only), then execs
# the command. Invoked via `sh` so it works regardless of the file's exec bit
# (the dev bind-mount overlays host file permissions).
ENTRYPOINT ["sh", "/app/docker-entrypoint.sh"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
