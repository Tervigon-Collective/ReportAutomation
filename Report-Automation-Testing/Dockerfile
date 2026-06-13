FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    cron \
    libpq-dev \
    gcc \
    g++ \
    libpango-1.0-0 \
    libharfbuzz0b \
    libpangoft2-1.0-0 \
    libfontconfig1 \
    libcairo2 \
    libgdk-pixbuf2.0-0 \
    shared-mime-info \
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
