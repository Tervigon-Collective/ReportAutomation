import os
import sys
import logging
import pytz
from datetime import datetime, timedelta

# Windows consoles often use cp1252, which cannot encode ₹ and other Unicode symbols.
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# Set up logging with cross-platform path that works in Azure Functions
def _get_log_dir():
    """Get a writable log directory, handling Azure Functions permissions."""
    # In Azure Functions, we can only write to /tmp
    if os.environ.get('WEBSITE_INSTANCE_ID'):  # Azure Functions environment
        log_dir = '/tmp/logs'
        try:
            os.makedirs(log_dir, exist_ok=True)
            return log_dir
        except (PermissionError, OSError):
            return None
    # Local development - use temp directory
    try:
        import tempfile
        temp_dir = tempfile.gettempdir()
        log_dir = os.path.join(temp_dir, 'logs')
        os.makedirs(log_dir, exist_ok=True)
        return log_dir
    except (PermissionError, OSError):
        # Last resort: try project directory (may fail in some environments)
        try:
            log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
            os.makedirs(log_dir, exist_ok=True)
            return log_dir
        except (PermissionError, OSError):
            return None

_log_dir = _get_log_dir()
if _log_dir:
    try:
        _log_file = os.path.join(_log_dir, 'timeframe_config.log')
        handlers = [
            logging.FileHandler(_log_file, encoding='utf-8'),
            logging.StreamHandler()
        ]
    except (PermissionError, OSError):
        # If file handler fails, just use stream handler
        handlers = [logging.StreamHandler()]
else:
    handlers = [logging.StreamHandler()]

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=handlers
)
logger = logging.getLogger(__name__)

# Set timezone to IST
IST = pytz.timezone('Asia/Kolkata')

# Module-level globals for universal reuse
_GLOBAL_START_DT = None  # tz-aware datetime in IST
_GLOBAL_END_DT = None    # tz-aware datetime in IST


def _ensure_tzaware_ist(dt: datetime) -> datetime:
    """Return dt as timezone-aware in IST; convert if it has a different tz, attach if naive."""
    if dt.tzinfo is None:
        return IST.localize(dt)
    return dt.astimezone(IST)


def _parse_date_input(value, is_end: bool) -> datetime | None:
    """
    Parse a value (str | datetime | None) into a tz-aware datetime in IST.
    - If str is in 'YYYY-MM-DD' format, set to 00:00:00 for start, 23:59:59 for end.
    - If already datetime, ensure IST timezone.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return _ensure_tzaware_ist(value)
    if isinstance(value, str):
        s = value.strip()
        # Try simple date first
        try:
            d = datetime.strptime(s, '%Y-%m-%d')
            if is_end:
                d = d.replace(hour=23, minute=59, second=59, microsecond=0)
            else:
                d = d.replace(hour=0, minute=0, second=0, microsecond=0)
            return _ensure_tzaware_ist(d)
        except Exception:
            pass
        # Try full datetime
        try:
            d = datetime.fromisoformat(s)
            return _ensure_tzaware_ist(d)
        except Exception:
            logger.warning("Failed to parse date string '%s'. Expected 'YYYY-MM-DD' or ISO format.", s)
            return None
    return None


def set_global_dates(start_date= None, end_date= None) -> None:
    """
    Set module-level global start and end datetimes (tz-aware IST) to be reused globally.
    Accepts str 'YYYY-MM-DD' or datetime values.
    """
    global _GLOBAL_START_DT, _GLOBAL_END_DT
    start_dt = _parse_date_input(start_date, is_end=False)
    end_dt = _parse_date_input(end_date, is_end=True)
    if start_dt is None or end_dt is None:
        logger.warning("set_global_dates received invalid values; globals not updated.")
        return
    if end_dt < start_dt:
        logger.info("End date precedes start date; swapping to maintain ordering.")
        start_dt, end_dt = end_dt, start_dt
    _GLOBAL_START_DT, _GLOBAL_END_DT = start_dt, end_dt
    logger.info("Global timeframe set: %s to %s", start_dt.strftime('%Y-%m-%d'), end_dt.strftime('%Y-%m-%d'))


def get_timeframe_config(start_date=None, end_date=None, days_range: int | None = None, use_fixed_dates: bool | None = None):
    """
    Provide a standardized timeframe configuration for global use.

    Resolution order (first non-null wins):
    1) Explicit start_date/end_date args
    2) Module globals set via set_global_dates()
    3) Environment variables ROLLUP_START_DATE / ROLLUP_END_DATE (YYYY-MM-DD)
    4) Today's date in IST (start at 00:00:00, end at 23:59:59)

    Returns a dict with: start_date (datetime), end_date (datetime), today (str), timestamp_str (str), days (int)
    """
    # 1) Explicit arguments
    start_dt = _parse_date_input(start_date, is_end=False)
    end_dt = _parse_date_input(end_date, is_end=True)

    # 2) Module globals
    if start_dt is None and _GLOBAL_START_DT is not None:
        start_dt = _GLOBAL_START_DT
    if end_dt is None and _GLOBAL_END_DT is not None:
        end_dt = _GLOBAL_END_DT

    # 3) Environment variables
    if start_dt is None:
        env_start = os.environ.get('ROLLUP_START_DATE')
        start_dt = _parse_date_input(env_start, is_end=False)
    if end_dt is None:
        env_end = os.environ.get('ROLLUP_END_DATE')
        end_dt = _parse_date_input(env_end, is_end=True)

    # 4) Default to today in IST (or N-day range if days_range provided)
    now = datetime.now(IST)
    if start_dt is None or end_dt is None:
        if days_range and isinstance(days_range, int) and days_range > 0:
            # Use last `days_range` days ending today (inclusive)
            start_candidate = (now - timedelta(days=days_range - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
            end_candidate = now.replace(hour=23, minute=59, second=59, microsecond=0)
            start_dt = start_dt or start_candidate
            end_dt = end_dt or end_candidate
        else:
            if start_dt is None:
                start_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
            if end_dt is None:
                end_dt = now.replace(hour=23, minute=59, second=59, microsecond=0)

    # Optional hardcoded test dates for reproducible testing
    # Enable via argument or env var USE_FIXED_DATES=true
    try:
        flag = use_fixed_dates
        if flag is None:
            flag_env = os.environ.get('USE_FIXED_DATES', '').strip().lower()
            flag = flag_env in ('1', 'true', 'yes')
        if flag:
            fixed_start_str = os.environ.get('FIXED_START_DATE', today)
            fixed_end_str = os.environ.get('FIXED_END_DATE', today)
            start_dt = _parse_date_input(fixed_start_str, is_end=False)
            end_dt = _parse_date_input(fixed_end_str, is_end=True)
    except Exception:
        pass

    # Ensure ordering
    if end_dt < start_dt:
        start_dt, end_dt = end_dt, start_dt

    # Persist the resolved timeframe to globals for future callers
    # Only set global dates if they weren't already set (to preserve our initialization)
    if _GLOBAL_START_DT is None or _GLOBAL_END_DT is None:
        set_global_dates(start_dt, end_dt)

    timestamp_str = f"{end_dt.strftime('%Y-%m-%d')} / {end_dt.strftime('%I:%M %p')} IST"
    num_days = (end_dt.date() - start_dt.date()).days + 1

    return {
        'start_date': start_dt,
        'end_date': end_dt,
        'today': end_dt.strftime('%Y-%m-%d'),
        'timestamp_str': timestamp_str,
        'days': num_days,
    }


def get_current_timestamp():
    """Return (today_str, timestamp_str) from the current timeframe configuration."""
    tf = get_timeframe_config()
    return tf['today'], tf['timestamp_str']


# Initialize default dates to current date when module is imported
from datetime import datetime
today = datetime.now().strftime('%Y-%m-%d')
set_global_dates(today, today)
