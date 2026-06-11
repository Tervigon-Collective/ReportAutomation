# ReportWeekMonth Azure Function

## Overview
This Azure Function generates and emails Week-to-Date (WTD) and Month-to-Date (MTD) reports on a scheduled basis.

## Trigger Schedule
- **Default Schedule**: `0 0 6 * * *` (Daily at 6:00 AM UTC / 11:30 AM IST)
- **Trigger Type**: Timer Trigger (CRON expression)

## CRON Schedule Format
The schedule uses NCRONTAB expressions with 6 fields:
```
{second} {minute} {hour} {day} {month} {day-of-week}
```

### Schedule Examples:
- `0 0 6 * * *` - Daily at 6:00 AM UTC
- `0 0 6 * * 1` - Every Monday at 6:00 AM UTC
- `0 0 6 1 * *` - First day of every month at 6:00 AM UTC
- `0 30 5 * * 1-5` - Weekdays (Mon-Fri) at 5:30 AM UTC

## Report Logic

### WTD (Week-to-Date):
- **Monday**: Shows previous week (Monday to Sunday)
- **Tuesday-Sunday**: Shows current week from Monday to yesterday

### MTD (Month-to-Date):
- **1st day of month**: Shows previous month (1st to last day)
- **2nd day onwards**: Shows current month from 1st to yesterday

## Configuration

### Modifying the Schedule
Edit the `schedule` field in `function.json`:
```json
{
  "schedule": "0 0 6 * * *"
}
```

### Function Settings
- `runOnStartup`: false - Function won't run when the function app starts
- `useMonitor`: true - Enables monitoring and ensures the function doesn't run multiple times
- `disabled`: false - Function is enabled

## Dependencies
- Azure Functions Core Tools
- Python 3.11+
- All dependencies from main `requirements.txt`

## Local Testing
```bash
# From project root
cd ReportWeekMonth
func start
```

## Deployment
This function will be deployed as part of the main Function App. Make sure to:
1. Update `host.json` if needed
2. Ensure all environment variables are configured in Azure
3. Deploy the entire function app

## Logs
View function execution logs in:
- Azure Portal → Function App → Functions → ReportWeekMonth → Monitor
- Application Insights (if configured)

## Email Configuration
Reports are automatically emailed to configured recipients using the email configuration from `env_config.py`.

