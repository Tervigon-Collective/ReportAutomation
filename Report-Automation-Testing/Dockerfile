FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    cron \
    libpq-dev \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN cp docker/crontab /etc/cron.d/report-jobs \
    && chmod 0644 /etc/cron.d/report-jobs \
    && chmod +x docker/entrypoint.sh \
    && mkdir -p /tmp/reports /tmp/logs

ENTRYPOINT ["docker/entrypoint.sh"]
