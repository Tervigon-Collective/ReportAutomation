#!/bin/bash
set -e

mkdir -p /tmp/reports /tmp/logs
touch /tmp/logs/hourly.log /tmp/logs/daily.log

exec cron -f
