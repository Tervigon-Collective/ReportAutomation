#!/bin/bash
set -e

mkdir -p /tmp/reports /tmp/logs
touch /tmp/logs/hourly.log /tmp/logs/daily.log /tmp/logs/cron.log

# Stream job logs to the container's stdout so `docker logs` works.
tail -n 0 -F /tmp/logs/hourly.log /tmp/logs/daily.log &

echo "[entrypoint] starting cron at $(date -Iseconds) (TZ=${TZ:-system})"
echo "[entrypoint] active schedules:"
grep -E '^[^#]' /etc/cron.d/report-jobs || true
exec cron -f
