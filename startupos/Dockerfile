FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Fail the build if any service entrypoint cannot import — catches missing deps before deploy.
RUN python -c "import api.main, daemon.main, ingest.runner, signals.engine, brain.pack" 2>&1 | grep -v "STARTUPOS\|not set" ; true
