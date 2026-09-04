FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e . 2>/dev/null || pip install --no-cache-dir fastapi uvicorn "pydantic>=2" "psycopg[binary]" python-dotenv anthropic slack_sdk httpx apscheduler
COPY . .
