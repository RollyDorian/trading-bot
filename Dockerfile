FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir .

CMD ["uvicorn", "trading_bot.dashboard.app:app", "--host", "0.0.0.0", "--port", "8000"]
