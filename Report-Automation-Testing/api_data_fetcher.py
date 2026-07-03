import requests
import pandas as pd
from sqlalchemy import create_engine, text
from database_manager import get_db_engine
from datetime import datetime, timedelta
import os  
import pytz
import json
import base64
from typing import Optional, Dict
from timeframe_config import get_timeframe_config
import logging
from revenue_gst import apply_net_revenue, apply_net_revenue_column, adjust_net_profit_single_day_payload

logger = logging.getLogger(__name__)  # Use module logger for diagnostic messages

BASE_URL = os.getenv('BACKEND_API_BASE_URL', "https://backend.seleric.com/api").strip()

# External SKU/Spend/Sales reporting service base
SKU_SPEND_SALES_BASE = os.getenv('SKU_SPEND_SALES_BASE', "https://skuspendsales-aghtewckaqbdfqep.centralindia-01.azurewebsites.net/api").strip()

"""Authentication for Node-Backend (JWT) and legacy Firebase hosts.

Node-Backend (`backend.seleric.com`) expects JWT from `/api/v1/auth/login`, not Firebase ID tokens.

Authentication priority for Node-Backend:
1. Cached JWT (auto-refreshed before expiry)
2. JWT_ACCESS_TOKEN from env (refreshed via BACKEND_REFRESH_TOKEN when expired)
3. POST /api/v1/auth/refresh-token with BACKEND_REFRESH_TOKEN
4. POST /api/v1/auth/login with BACKEND_EMAIL + BACKEND_PASSWORD, remember_me=true
5. POST /api/v1/auth/select-context with DASHBOARD_COMPANY_ID + CLICKHOUSE_BRAND_ID

Legacy Firebase (old hosts):
1. FIREBASE_ID_TOKEN
2. Firebase email/password via Identity Toolkit
"""
API_BEARER_TOKEN = os.getenv('FIREBASE_ID_TOKEN', '').strip()
JWT_ACCESS_TOKEN = os.getenv('JWT_ACCESS_TOKEN', os.getenv('BACKEND_ACCESS_TOKEN', '')).strip()
BACKEND_REFRESH_TOKEN = os.getenv('BACKEND_REFRESH_TOKEN', '').strip()
BACKEND_CSRF_TOKEN = os.getenv('BACKEND_CSRF_TOKEN', '').strip()
BACKEND_REMEMBER_ME = os.getenv('BACKEND_REMEMBER_ME', 'true').lower() in ('1', 'true', 'yes')
BACKEND_EMAIL = os.getenv('BACKEND_EMAIL', os.getenv('FIREBASE_EMAIL', '')).strip()
BACKEND_PASSWORD = os.getenv('BACKEND_PASSWORD', os.getenv('FIREBASE_PASSWORD', '')).strip()
FIREBASE_TOKEN_CACHE = {'token': None, 'expires_at': None}
JWT_TOKEN_CACHE = {
    'token': None,
    'refresh_token': None,
    'expires_at': None,
    'session': None,
    'login_failed': False,
}

# Firebase credentials for automatic token generation from environment
FIREBASE_WEB_API_KEY = os.getenv('FIREBASE_WEB_API_KEY', '').strip()
FIREBASE_EMAIL = os.getenv('FIREBASE_EMAIL', '').strip()
FIREBASE_PASSWORD = os.getenv('FIREBASE_PASSWORD', '').strip()

def set_api_bearer_token(token: str):
    """Set the Firebase ID token directly."""
    global API_BEARER_TOKEN
    API_BEARER_TOKEN = token

def set_firebase_token(token: str):
    """Alias for setting Firebase ID token from external callers (frontend bridge)."""
    set_api_bearer_token(token)

def generate_firebase_id_token_from_credentials() -> Optional[str]:
    """Generate Firebase ID token using email/password credentials via REST API.
    
    Uses FIREBASE_WEB_API_KEY, FIREBASE_EMAIL, and FIREBASE_PASSWORD environment variables.
    
    Returns:
        Firebase ID token string, or None if generation fails
    """
    if not all([FIREBASE_WEB_API_KEY, FIREBASE_EMAIL, FIREBASE_PASSWORD]):
        return None
    
    try:
        # Check if we have a cached valid token
        if (FIREBASE_TOKEN_CACHE['token'] and 
            FIREBASE_TOKEN_CACHE['expires_at'] and 
            datetime.now() < FIREBASE_TOKEN_CACHE['expires_at']):
            return FIREBASE_TOKEN_CACHE['token']
        
        url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={FIREBASE_WEB_API_KEY}"
        payload = {
            "email": FIREBASE_EMAIL,
            "password": FIREBASE_PASSWORD,
            "returnSecureToken": True
        }
        
        try:
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
        except requests.exceptions.HTTPError as e:
            # Log HTTP failure details to help debugging (show limited response text)
            logger.warning(f"[API] Firebase auth failed (HTTP {getattr(response, 'status_code', None)}): {getattr(response, 'text', '')[:300]}")
            return None
        except Exception as e:
            logger.exception("[API] Error while requesting Firebase token: %s", e)
            return None
        
        result = response.json()
        id_token = result.get('idToken')
        
        if not id_token:
            logger.warning("[API] Error: No ID token in Firebase signIn response: %s", result)
            return None
        
        # Cache the token (tokens typically expire in 1 hour)
        expires_in = int(result.get('expiresIn', 3600))
        expires_at = datetime.now() + timedelta(seconds=expires_in - 120)  # Refresh 2 minutes early
        
        FIREBASE_TOKEN_CACHE['token'] = id_token
        FIREBASE_TOKEN_CACHE['expires_at'] = expires_at
        logger.info("[API] Generated Firebase ID token via email/password (expires in %ds)", expires_in)
        
        return id_token
        
    except requests.exceptions.HTTPError as e:
        return None
    except Exception as e:
        return None

def _uses_node_backend_jwt() -> bool:
    host = BASE_URL.lower()
    return 'backend.seleric' in host or 'localhost' in host or '127.0.0.1' in host


def _api_root() -> str:
    return BASE_URL.rstrip('/')


def _decode_jwt_claims(token: str) -> dict:
    try:
        payload = token.split('.')[1]
        payload += '=' * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _jwt_expires_at(token: str) -> Optional[datetime]:
    exp = _decode_jwt_claims(token).get('exp')
    if exp is None:
        return None
    try:
        return datetime.fromtimestamp(int(exp))
    except (TypeError, ValueError, OSError):
        return None


def _jwt_is_expired(token: str, leeway_seconds: int = 120) -> bool:
    exp = _jwt_expires_at(token)
    if exp is None:
        return False
    return datetime.now() >= (exp - timedelta(seconds=leeway_seconds))


def _backend_csrf_headers(session: Optional[requests.Session] = None) -> dict:
    csrf = BACKEND_CSRF_TOKEN
    if not csrf and session is not None:
        csrf = session.cookies.get('csrf_token')
    return {'x-csrf-token': csrf} if csrf else {}


def _prime_backend_session(session: requests.Session) -> str:
    session.get(f"{_api_root()}/v1/version", timeout=15)
    return session.cookies.get('csrf_token') or BACKEND_CSRF_TOKEN or ''


def _store_backend_tokens(
    access_token: str,
    refresh_token: Optional[str] = None,
    session: Optional[requests.Session] = None,
) -> str:
    JWT_TOKEN_CACHE['token'] = access_token
    JWT_TOKEN_CACHE['expires_at'] = _jwt_expires_at(access_token) or (
        datetime.now() + timedelta(minutes=50)
    )
    if refresh_token:
        JWT_TOKEN_CACHE['refresh_token'] = refresh_token
    if session is not None:
        JWT_TOKEN_CACHE['session'] = session
    JWT_TOKEN_CACHE['login_failed'] = False
    return access_token


def refresh_backend_jwt() -> Optional[str]:
    """Refresh access token using BACKEND_REFRESH_TOKEN or cached refresh token."""
    refresh = JWT_TOKEN_CACHE.get('refresh_token') or BACKEND_REFRESH_TOKEN
    if not refresh:
        return None

    session = JWT_TOKEN_CACHE.get('session') or requests.Session()
    try:
        _prime_backend_session(session)
        headers = _backend_csrf_headers(session)
        resp = session.post(
            f"{_api_root()}/v1/auth/refresh-token",
            json={'refresh_token': refresh},
            headers=headers,
            timeout=20,
        )
        if resp.status_code != 200:
            logger.warning(
                "[API] refresh-token failed (HTTP %s): %s",
                resp.status_code,
                resp.text[:300],
            )
            return None
        data = (resp.json() or {}).get('data') or resp.json() or {}
        token = data.get('access_token')
        if not token:
            return None
        new_refresh = data.get('refresh_token') or refresh
        logger.info("[API] Refreshed Node-Backend JWT (exp=%s)", _jwt_expires_at(token))
        return _store_backend_tokens(token, new_refresh, session)
    except Exception as e:
        logger.warning("[API] refresh-token error: %s", e)
        return None


def generate_backend_jwt_from_login() -> Optional[str]:
    """Obtain Node-Backend JWT via /auth/login (+ optional select-context)."""
    if not BACKEND_EMAIL or not BACKEND_PASSWORD:
        return None

    if JWT_TOKEN_CACHE.get('login_failed'):
        return None

    if (
        JWT_TOKEN_CACHE.get('token')
        and JWT_TOKEN_CACHE.get('expires_at')
        and datetime.now() < JWT_TOKEN_CACHE['expires_at']
    ):
        return JWT_TOKEN_CACHE['token']

    session = requests.Session()
    try:
        _prime_backend_session(session)
        headers = _backend_csrf_headers(session)
        login_resp = session.post(
            f"{_api_root()}/v1/auth/login",
            json={
                'email': BACKEND_EMAIL,
                'password': BACKEND_PASSWORD,
                'remember_me': BACKEND_REMEMBER_ME,
            },
            headers=headers,
            timeout=20,
        )
        if login_resp.status_code != 200:
            JWT_TOKEN_CACHE['login_failed'] = True
            logger.warning(
                "[API] Backend login failed (HTTP %s): %s",
                login_resp.status_code,
                login_resp.text[:300],
            )
            return None
        payload = login_resp.json()
        data = payload.get('data') or payload
        token = data.get('access_token')
        refresh = data.get('refresh_token') or BACKEND_REFRESH_TOKEN
        if not token:
            logger.warning("[API] Backend login response missing access_token")
            return None

        company_id = int(os.getenv("DASHBOARD_COMPANY_ID", os.getenv("API_COMPANY_ID", "19")))
        brand_id = int(os.getenv("CLICKHOUSE_BRAND_ID", os.getenv("API_BRAND_ID", "20")))
        ctx_headers = {**headers, 'Authorization': f'Bearer {token}'}
        ctx_resp = session.post(
            f"{_api_root()}/v1/auth/select-context",
            json={'company_id': company_id, 'brand_id': brand_id},
            headers=ctx_headers,
            timeout=20,
        )
        if ctx_resp.status_code == 200:
            ctx_data = (ctx_resp.json() or {}).get('data') or ctx_resp.json() or {}
            token = ctx_data.get('access_token') or ctx_data.get('token') or token
            refresh = ctx_data.get('refresh_token') or refresh
            logger.info("[API] Backend JWT with company=%s brand=%s", company_id, brand_id)
        else:
            logger.warning(
                "[API] select-context failed (HTTP %s); using login token",
                ctx_resp.status_code,
            )

        logger.info("[API] Obtained Node-Backend JWT via login (exp=%s)", _jwt_expires_at(token))
        return _store_backend_tokens(token, refresh, session)
    except Exception as e:
        logger.warning("[API] Backend JWT login error: %s", e)
        return None


def get_backend_jwt_token(force_refresh: bool = False) -> Optional[str]:
    """Return a valid JWT for Node-Backend API calls (auto-refresh on expiry)."""
    if not force_refresh:
        cached = JWT_TOKEN_CACHE.get('token')
        if cached and not _jwt_is_expired(cached):
            return cached

        if JWT_ACCESS_TOKEN and not _jwt_is_expired(JWT_ACCESS_TOKEN):
            return _store_backend_tokens(
                JWT_ACCESS_TOKEN,
                JWT_TOKEN_CACHE.get('refresh_token') or BACKEND_REFRESH_TOKEN,
            )

        if JWT_ACCESS_TOKEN and _jwt_is_expired(JWT_ACCESS_TOKEN):
            refreshed = refresh_backend_jwt()
            if refreshed:
                return refreshed

    refreshed = refresh_backend_jwt()
    if refreshed:
        return refreshed

    return generate_backend_jwt_from_login()


def get_firebase_token() -> Optional[str]:
    """Get bearer token (Node-Backend JWT or legacy Firebase ID token)."""
    if _uses_node_backend_jwt():
        token = get_backend_jwt_token()
        if token:
            return token
        logger.warning(
            "[API] No Node-Backend JWT. Set JWT_ACCESS_TOKEN or valid BACKEND_EMAIL/BACKEND_PASSWORD."
        )
        return None

    # Legacy Firebase path
    # Priority 1: Direct token from environment
    if API_BEARER_TOKEN:
        logger.info("[API] Using FIREBASE_ID_TOKEN from environment (len=%d)", len(API_BEARER_TOKEN))
        return API_BEARER_TOKEN
    
    # Priority 2: Auto-generate using email/password credentials from environment
    # Only attempt if all required credentials are provided
    if FIREBASE_WEB_API_KEY and FIREBASE_EMAIL and FIREBASE_PASSWORD:
        auto_token = generate_firebase_id_token_from_credentials()
        if auto_token:
            # Set it globally so it can be reused
            set_api_bearer_token(auto_token)
            logger.info("[API] Auto-generated Firebase ID token via email/password (len=%d)", len(auto_token))
            return auto_token
        else:
            logger.warning("[API] Auto-generation of Firebase token failed. Check FIREBASE_WEB_API_KEY, FIREBASE_EMAIL, and FIREBASE_PASSWORD environment variables.")
    else:
        missing = []
        if not FIREBASE_WEB_API_KEY: missing.append("FIREBASE_WEB_API_KEY")
        if not FIREBASE_EMAIL: missing.append("FIREBASE_EMAIL")
        if not FIREBASE_PASSWORD: missing.append("FIREBASE_PASSWORD")
        if missing and not API_BEARER_TOKEN:
            logger.warning(f"[API] Cannot generate token. Missing environment variables: {', '.join(missing)}")
    
    logger.warning("[API] No Firebase token available from environment variables")
    return None

def initialize_firebase_auth(service_account_dict: Optional[Dict] = None):
    """Initializes authentication parameters (legacy placeholder)."""
    pass


def clear_token_cache():
    """Clear cached auth tokens (useful when token is invalid/expired)."""
    global FIREBASE_TOKEN_CACHE, JWT_TOKEN_CACHE
    FIREBASE_TOKEN_CACHE['token'] = None
    FIREBASE_TOKEN_CACHE['expires_at'] = None
    JWT_TOKEN_CACHE['token'] = None
    JWT_TOKEN_CACHE['refresh_token'] = None
    JWT_TOKEN_CACHE['expires_at'] = None
    JWT_TOKEN_CACHE['session'] = None
    JWT_TOKEN_CACHE['login_failed'] = False
    print("[API] Token cache cleared")

def make_authenticated_request(method: str, url: str, retry_on_401: bool = True, max_retries: int = 1, **kwargs) -> requests.Response:
    """Make an authenticated HTTP request with automatic token refresh on 401.
    
    Args:
        method: HTTP method ('GET', 'POST', etc.)
        url: Request URL
        retry_on_401: If True, automatically retry after token refresh on 401
        max_retries: Number of times to retry on 401
        **kwargs: Additional arguments passed to requests.request()
    
    Returns:
        requests.Response object
    """
    attempt = 0
    while attempt <= max_retries:
        # Update/Get headers with current token
        headers = kwargs.get('headers', {}).copy()
        auth_headers = get_api_headers()
        headers.update(auth_headers)
        kwargs['headers'] = headers
        
        try:
            response = requests.request(method, url, **kwargs)
            
            # If successful, return the response
            if response.status_code < 400:
                return response
                
            # Handle 401 Unauthorized — only retry if we had a token to refresh
            if response.status_code == 401 and retry_on_401 and attempt < max_retries:
                had_token = bool(headers.get('Authorization'))
                if had_token:
                    logger.warning(
                        "[API] 401 on attempt %d — refreshing JWT and retrying...",
                        attempt + 1,
                    )
                    clear_token_cache()
                    if _uses_node_backend_jwt():
                        get_backend_jwt_token(force_refresh=True)
                    attempt += 1
                    import time
                    time.sleep(1)
                    continue
                return response
            
            # For other errors, just return the response and let the caller handle it
            return response
            
        except requests.exceptions.RequestException as e:
            logger.error(f"[API] Request exception: {str(e)}")
            if attempt < max_retries:
                attempt += 1
                import time
                time.sleep(1)
                continue
            raise e
            
    return response

def get_api_headers():
    """Get API headers with authentication token.
    
    Automatically generates token if not available using credentials or environment variables.
    Tokens are cached and reused until expiration.
    
    Returns:
        dict: Headers with Authorization Bearer token, or empty dict if no token available
    """
    try:
        token = get_firebase_token()
        if token:
            return {'Authorization': f'Bearer {token}'}
        else:
            logger.warning("[API] No Firebase token available. Token generation failed. Check credentials or set FIREBASE_ID_TOKEN. Requests may fail with 401 Unauthorized.")
            return {}
    except Exception as e:
        logger.exception("[API] Error getting API headers: %s", e)
        return {}


def debug_firebase_token_status():
    """Return diagnostic info about Firebase token state and source."""
    status = {
        'api_bearer_token_set': bool(API_BEARER_TOKEN),
        'token_cached': bool(FIREBASE_TOKEN_CACHE.get('token')),
        'token_expires_at': FIREBASE_TOKEN_CACHE.get('expires_at'),
        'fired_methods': []
    }
    # Probe token acquisition methods without modifying cache where possible
    if API_BEARER_TOKEN:
        status['fired_methods'].append('env')
    if FIREBASE_TOKEN_FILE:
        status['fired_methods'].append('file')
    # We won't auto-generate here to avoid side effects; just report availability of credentials
    if FIREBASE_WEB_API_KEY and FIREBASE_EMAIL and FIREBASE_PASSWORD:
        status['fired_methods'].append('credentials_available')
    if FIREBASE_SERVICE_ACCOUNT_INFO or os.getenv('FIREBASE_SERVICE_ACCOUNT_JSON'):
        status['fired_methods'].append('service_account_available')
    return status


def get_today_date_ist():
    """Get today's date in IST timezone"""
    try:
        # Define IST timezone
        ist = pytz.timezone('Asia/Kolkata')

        # Get current UTC time and convert to IST
        utc_now = datetime.utcnow().replace(tzinfo=pytz.utc)  # Get current UTC time with timezone info
        today = utc_now.astimezone(ist)  # Convert UTC to IST

        # Return the date in 'YYYY-MM-DD' format
        return today.strftime('%Y-%m-%d')
    except Exception as e:
        logger.exception("Error getting IST date: %s", e)
        # Return None in case of error
        return None


def get_today_ist_hour_bounds():
    """Return ('YYYY-MM-DD 00', 'YYYY-MM-DD 23') for today's date in IST."""
    try:
        ist = pytz.timezone('Asia/Kolkata')
        utc_now = datetime.utcnow().replace(tzinfo=pytz.utc)
        today_ist = utc_now.astimezone(ist)
        date_prefix = today_ist.strftime('%Y-%m-%d')
        return f"{date_prefix} 00", f"{date_prefix} 23"
    except Exception as e:
        print(f"Error computing IST hour bounds: {e}")
        return None, None


def fetch_sales(start_date: Optional[str] = None, end_date: Optional[str] = None):
    """
    Deprecated in favor of hourly endpoint `sales_unitCost_by_hour`. Kept for backward compatibility.
    """
    try:
        tf = get_timeframe_config(start_date=start_date, end_date=end_date)
        start_str = tf['start_date'].strftime('%Y-%m-%d')
        end_str = tf['end_date'].strftime('%Y-%m-%d')
        resp = requests.get(
            f"{BASE_URL}/sales",
            params={'startDate': start_str, 'endDate': end_str},
            headers=get_api_headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            data = dict(data)
            for k in ('metaSales', 'googleSales', 'organicSales', 'totalSales'):
                if k in data:
                    data[k] = apply_net_revenue(float(data.get(k, 0) or 0))
        return data
    except Exception as e:
        print(f"Error fetching sales data: {e}")
        return {"metaSales": 0, "googleSales": 0, "organicSales": 0, "totalSales": 0}

def fetch_ad_spend(start_date: Optional[str] = None, end_date: Optional[str] = None):
    """
    Deprecated in favor of hourly endpoint `ad_spend_by_hour`. Kept for backward compatibility.
    Uses /v1/historical/dashboard on Node-Backend when legacy /ad_spend is unavailable.
    """
    try:
        tf = get_timeframe_config(start_date=start_date, end_date=end_date)
        start_str = tf['start_date'].strftime('%Y-%m-%d')
        end_str = tf['end_date'].strftime('%Y-%m-%d')
        if _prefer_api():
            dash = fetch_historical_dashboard(start_str, end_str) or {}
            breakdown = dash.get('ad_spend_breakdown') or {}
            meta_spend = float((breakdown.get('meta') or 0) or 0)
            google_spend = float((breakdown.get('google') or 0) or 0)
            amazon_block = breakdown.get('amazon') or {}
            amazon_spend = float(amazon_block.get('spend', amazon_block.get('ad_spend', 0)) or 0) if isinstance(amazon_block, dict) else 0.0
            total = float(dash.get('total_ad_spend', meta_spend + google_spend + amazon_spend) or 0)
            return {
                'googleSpend': google_spend,
                'facebookSpend': meta_spend,
                'amazonSpend': amazon_spend,
                'totalSpend': total,
            }
        resp = requests.get(
            f"{BASE_URL}/ad_spend",
            params={'startDate': start_str, 'endDate': end_str},
            headers=get_api_headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        try:
            print(f"[AdSpend] start_date={start_str} end_date={end_str} payload={data}")
        except Exception:
            pass
        return data
    except Exception as e:
        print(f"Error fetching ad spend data: {e}")
        return {"googleSpend": 0, "facebookSpend": 0, "totalSpend": 0}


def fetch_ad_spend_by_hour(start_datetime: Optional[str] = None, end_datetime: Optional[str] = None):
    """
    Fetch hourly ad spend for a range. Params must be 'YYYY-MM-DD HH'.
    Falls back to today's IST day bounds if not provided.
    """
    try:
        if not start_datetime or not end_datetime:
            start_datetime, end_datetime = get_today_ist_hour_bounds()
        resp = requests.get(
            f"{BASE_URL}/ad_spend_by_hour",
            params={
                'startDateTime': start_datetime,
                'endDateTime': end_datetime,
            },
            headers=get_api_headers(),
        )
        resp.raise_for_status()
        return resp.json() or {}
    except Exception as e:
        print(f"Error fetching hourly ad spend: {e}")
        return {"hourlyAdSpend": [], "totals": {"facebookSpend": 0, "googleSpend": 0}}


def _apply_gst_to_sales_unitcost_payload(data: Optional[dict]) -> dict:
    """Scale revenue fields in sales_unitCost_by_hour JSON (sum bucket)."""
    if not data:
        return data or {}
    out = dict(data)
    sm = out.get('sum')
    if isinstance(sm, dict):
        sm = dict(sm)
        for key in (
            'meta_sales', 'google_sales', 'organic_sales', 'total_sales',
            'metaSales', 'googleSales', 'organicSales', 'totalSales',
        ):
            if key in sm:
                sm[key] = apply_net_revenue(float(sm.get(key, 0) or 0))
        out['sum'] = sm
    return out


def fetch_sales_unitcost_by_hour(start_datetime: Optional[str] = None, end_datetime: Optional[str] = None):
    """
    Fetch hourly sales and unit cost for a range. Params must be 'YYYY-MM-DD HH'.
    Falls back to today's IST day bounds if not provided.
    """
    try:
        if not start_datetime or not end_datetime:
            start_datetime, end_datetime = get_today_ist_hour_bounds()
        resp = requests.get(
            f"{BASE_URL}/sales_unitCost_by_hour",
            params={
                'startDateTime': start_datetime,
                'endDateTime': end_datetime,
            },
            headers=get_api_headers(),
        )
        resp.raise_for_status()
        return _apply_gst_to_sales_unitcost_payload(resp.json() or {})
    except Exception as e:
        print(f"Error fetching hourly sales/unit cost: {e}")
        return {"hourlySales": [], "sum": {"total_sales": 0, "total_unit_cost": 0}}


def fetch_google_spend(start_date: Optional[str] = None, end_date: Optional[str] = None) -> float:
    """
    Convenience wrapper to return only Google spend (float) for the date range.
    """
    try:
        data = fetch_ad_spend(start_date=start_date, end_date=end_date) or {}
        val = float(data.get('googleSpend', 0) or 0)
        return val
    except Exception:
        return 0.0

def fetch_net_profit(start_date: Optional[str] = None, end_date: Optional[str] = None):
    try:
        tf = get_timeframe_config(start_date=start_date, end_date=end_date)
        start_str = tf['start_date'].strftime('%Y-%m-%d')
        end_str = tf['end_date'].strftime('%Y-%m-%d')
        resp = requests.get(
            f"{BASE_URL}/net_profit",
            params={'startDate': start_str, 'endDate': end_str},
            headers=get_api_headers(),
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"Error fetching net profit data: {e}")
        return {"metaNetProfit": 0, "googleNetProfit": 0, "totalNetProfit": 0}

def fetch_net_profit_single_day(start_date: Optional[str] = None, end_date: Optional[str] = None):
    """
    Fetch daily breakdown of net profit data for a date range from the backend API.
    
    API Endpoint: /api/net_profit_single_day?startDate=YYYY-MM-DD&endDate=YYYY-MM-DD
    """
    try:
        tf = get_timeframe_config(start_date=start_date, end_date=end_date)
        start_str = tf['start_date'].strftime('%Y-%m-%d')
        end_str = tf['end_date'].strftime('%Y-%m-%d')
        
        logger.info(f"[API] Calling /net_profit_single_day with startDate={start_str}, endDate={end_str}")
        
        resp = make_authenticated_request(
            'GET',
            f"{BASE_URL}/net_profit_single_day", 
            params={'startDate': start_str, 'endDate': end_str}
        )
        
        if resp.status_code == 200:
            json_response = resp.json()
            logger.info(f"[API] Successfully fetched net profit data for range: {json_response.get('data', {}).get('dateRange', {})}")
            return adjust_net_profit_single_day_payload(json_response)
        else:
            logger.error(f"[API] Failed to fetch net profit data: {resp.status_code} - {resp.text[:200]}")
            
    except Exception as e:
        logger.exception(f"[API] Error fetching net profit single day data: {str(e)}")
    
    # Return default response on failure
    return {
        "success": False,
        "data": {
            "dateRange": {"startDate": start_str if 'start_str' in locals() else '', 
                         "endDate": end_str if 'end_str' in locals() else '', 
                         "days": 0},
            "dailyBreakdowns": [],
            "totals": {"revenue": 0, "cogs": 0, "adSpend": 0, "netProfit": 0}
        }
    }

def fetch_cogs(start_date: Optional[str] = None, end_date: Optional[str] = None):
    try:
        tf = get_timeframe_config(start_date=start_date, end_date=end_date)
        start_str = tf['start_date'].strftime('%Y-%m-%d')
        end_str = tf['end_date'].strftime('%Y-%m-%d')
        resp = requests.get(
            f"{BASE_URL}/cogs",
            params={'startDate': start_str, 'endDate': end_str},
            headers=get_api_headers(),
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"Error fetching COGS data: {e}")
        return {"metaCogs": 0, "googleCogs": 0, "totalCogs": 0}

def fetch_roas(start_date: Optional[str] = None, end_date: Optional[str] = None):
    try:
        tf = get_timeframe_config(start_date=start_date, end_date=end_date)
        start_str = tf['start_date'].strftime('%Y-%m-%d')
        end_str = tf['end_date'].strftime('%Y-%m-%d')
        resp = requests.get(
            f"{BASE_URL}/roas",
            params={'startDate': start_str, 'endDate': end_str},
            headers=get_api_headers(),
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"Error fetching ROAS data: {e}")
        return {"meta": {"grossRoas": 0, "netRoas": 0, "beRoas": 1}, "google": {"grossRoas": 0, "netRoas": 0, "beRoas": 1}, "total": {"grossRoas": 0, "netRoas": 0, "beRoas": 1}}

def fetch_order_count(start_date: Optional[str] = None, end_date: Optional[str] = None):
    try:
        tf = get_timeframe_config(start_date=start_date, end_date=end_date)
        start_str = tf['start_date'].strftime('%Y-%m-%d')
        end_str = tf['end_date'].strftime('%Y-%m-%d')
        resp = requests.get(
            f"{BASE_URL}/order_count",
            params={'startDate': start_str, 'endDate': end_str},
            headers=get_api_headers(),
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"Error fetching order count data: {e}")
        return {"orderCount": 0, "totalQuantity": 0, "metaQuantity": 0, "googleQuantity": 0, "organicQuantity": 0}

def fetch_net_profit_from_db(n_days: int = 30) -> dict:
    """
    Calculate net profit directly from the database for the last n_days.

    Sources chosen to match what the backend API computes:
        revenue_ex_gst : shopify_orders.total_price_amount / GST_DIVISOR (all non-cancelled orders)
        cogs           : SUM(line_item.quantity * variant.unit_cost_amount) — matches API exactly
        meta_spend     : SUM(ads_insights_hourly.spend) — raw Meta hourly table, matches API Facebook spend
        google_spend   : SUM(dw_google_ads_attribution.cost_amount) — comprehensive Google spend
                         NOTE: the API backend has incomplete Google spend data (shows 0 on most days);
                         this DB source is more accurate for Google.

    Returns:
        {
            "dailyBreakdown": DataFrame with columns
                [sale_date, revenue, cogs, meta_spend, google_spend, total_ad_spend, net_profit],
            "totals": {
                "revenue": float, "cogs": float,
                "metaSpend": float, "googleSpend": float,
                "adSpend": float, "netProfit": float,
            }
        }
    """
    try:
        engine = get_db_engine()

        query = text("""
            WITH date_series AS (
                SELECT generate_series(
                    CURRENT_DATE - CAST(:n_days - 1 AS int) * INTERVAL '1 day',
                    CURRENT_DATE,
                    INTERVAL '1 day'
                )::date AS sale_date
            ),
            revenue AS (
                SELECT
                    DATE(processed_at_ist) AS sale_date,
                    SUM(total_price_amount) AS gross_revenue
                FROM shopify_orders
                WHERE cancelled_at_ist IS NULL
                  AND DATE(processed_at_ist) >= CURRENT_DATE - CAST(:n_days - 1 AS int) * INTERVAL '1 day'
                GROUP BY 1
            ),
            cogs AS (
                SELECT
                    DATE(o.processed_at_ist) AS sale_date,
                    SUM(li.quantity * COALESCE(spv.unit_cost_amount, 0)) AS total_cogs
                FROM shopify_order_line_items li
                JOIN shopify_orders o ON li.order_id = o.order_id
                JOIN shopify_product_variants spv ON li.variant_id = spv.variant_id
                WHERE o.cancelled_at_ist IS NULL
                  AND DATE(o.processed_at_ist) >= CURRENT_DATE - CAST(:n_days - 1 AS int) * INTERVAL '1 day'
                GROUP BY 1
            ),
            -- Use ads_insights_hourly for Meta spend (raw Meta table) — matches API Facebook spend.
            -- dw_meta_ads_attribution.spend only covers campaigns with attributed orders (~40% of actual spend).
            meta_spend AS (
                SELECT
                    date_start AS sale_date,
                    SUM(spend) AS meta_spend
                FROM ads_insights_hourly
                WHERE date_start >= CURRENT_DATE - CAST(:n_days - 1 AS int) * INTERVAL '1 day'
                GROUP BY 1
            ),
            google_spend AS (
                SELECT
                    date_start AS sale_date,
                    SUM(cost_amount) AS google_spend
                FROM dw_google_ads_attribution
                WHERE date_start >= CURRENT_DATE - CAST(:n_days - 1 AS int) * INTERVAL '1 day'
                GROUP BY 1
            )
            SELECT
                d.sale_date,
                COALESCE(r.gross_revenue, 0)     AS gross_revenue,
                COALESCE(c.total_cogs, 0)        AS cogs,
                COALESCE(ms.meta_spend, 0)       AS meta_spend,
                COALESCE(gs.google_spend, 0)     AS google_spend
            FROM date_series d
            LEFT JOIN revenue r       ON d.sale_date = r.sale_date
            LEFT JOIN cogs c          ON d.sale_date = c.sale_date
            LEFT JOIN meta_spend ms   ON d.sale_date = ms.sale_date
            LEFT JOIN google_spend gs ON d.sale_date = gs.sale_date
            ORDER BY d.sale_date
        """)

        df = pd.read_sql(query, engine, params={"n_days": n_days})

        if df.empty:
            logger.warning("[DB] fetch_net_profit_from_db: no data found for last %d days", n_days)
            return {
                "dailyBreakdown": df,
                "totals": {"revenue": 0, "cogs": 0, "metaSpend": 0, "googleSpend": 0, "adSpend": 0, "netProfit": 0},
            }

        df = df.copy()
        df["revenue"] = apply_net_revenue_column(df["gross_revenue"])
        df["cogs"] = pd.to_numeric(df["cogs"], errors="coerce").fillna(0)
        df["meta_spend"] = pd.to_numeric(df["meta_spend"], errors="coerce").fillna(0)
        df["google_spend"] = pd.to_numeric(df["google_spend"], errors="coerce").fillna(0)
        df["total_ad_spend"] = df["meta_spend"] + df["google_spend"]
        df["net_profit"] = df["revenue"] - df["cogs"] - df["total_ad_spend"]
        df = df.drop(columns=["gross_revenue"])

        totals = {
            "revenue": float(df["revenue"].sum()),
            "cogs": float(df["cogs"].sum()),
            "metaSpend": float(df["meta_spend"].sum()),
            "googleSpend": float(df["google_spend"].sum()),
            "adSpend": float(df["total_ad_spend"].sum()),
            "netProfit": float(df["net_profit"].sum()),
        }

        logger.info(
            "[DB] Net profit (last %d days): revenue=%.2f cogs=%.2f adSpend=%.2f netProfit=%.2f",
            n_days, totals["revenue"], totals["cogs"], totals["adSpend"], totals["netProfit"],
        )
        return {"dailyBreakdown": df, "totals": totals}

    except Exception as e:
        logger.exception("[DB] Error in fetch_net_profit_from_db: %s", e)
        return {
            "dailyBreakdown": pd.DataFrame(
                columns=["sale_date", "revenue", "cogs", "meta_spend", "google_spend", "total_ad_spend", "net_profit"]
            ),
            "totals": {"revenue": 0, "cogs": 0, "metaSpend": 0, "googleSpend": 0, "adSpend": 0, "netProfit": 0},
        }


def fetch_db_sales(n_days=30):
    """
    Fetch sales data from database using the provided SQL query.
    Returns the last `n_days` of sales data.
    """
    try:
        # Use centralized database engine
        engine = get_db_engine()
        
        query = f"""
        WITH daily_sales AS (
            SELECT
                DATE(o.processed_at_ist) AS sale_date,
                SUM(o.total_price_amount) AS total_sales
            FROM shopify_orders o
            WHERE o.cancelled_at_ist IS NULL
            GROUP BY DATE(o.processed_at_ist)
        ),
        ranked_days AS (
            SELECT *,
                   ROW_NUMBER() OVER (ORDER BY sale_date DESC) AS rn
            FROM daily_sales
        )
        SELECT sale_date, total_sales
        FROM ranked_days
        WHERE rn <= {n_days}
        ORDER BY sale_date;
        """
        
        result = pd.read_sql(query, engine)
        if not result.empty and 'total_sales' in result.columns:
            result = result.copy()
            result['total_sales'] = apply_net_revenue_column(result['total_sales'])
        return result
    except Exception as e:
        print(f"Database error in fetch_db_sales: {str(e)}")
        # Return empty DataFrame if query fails
        return pd.DataFrame(columns=['sale_date', 'total_sales'])


def fetch_shopify_sales_orders_detail(start_date: str, end_date: str) -> pd.DataFrame:
    """
    Order-level Shopify sales for the report date range (inclusive).
    Uses processed_at_ist date; excludes cancelled orders. Columns use state/city aliases.
    """
    try:
        engine = get_db_engine()
        query = text("""
            SELECT
                order_id,
                order_name,
                DATE(processed_at_ist) AS sale_date,
                total_price_amount,
                ship_province AS state,
                ship_city AS city,
                display_fulfillment_status,
                ship_country
            FROM public.shopify_orders o
            WHERE o.cancelled_at_ist IS NULL
              AND DATE(processed_at_ist) BETWEEN :start_date AND :end_date
            ORDER BY DATE(processed_at_ist), order_id
        """)
        df = pd.read_sql(
            query, engine, params={"start_date": start_date, "end_date": end_date}
        )
        if not df.empty and 'total_price_amount' in df.columns:
            df = df.copy()
            df['total_price_amount'] = apply_net_revenue_column(df['total_price_amount'])
        return df
    except Exception as e:
        logger.warning("fetch_shopify_sales_orders_detail: %s", e)
        return pd.DataFrame(
            columns=[
                "order_id",
                "order_name",
                "sale_date",
                "total_price_amount",
                "state",
                "city",
                "display_fulfillment_status",
                "ship_country",
            ]
        )


def fetch_net_profit_series_from_api(start_date: str, end_date: str) -> pd.DataFrame:
    """Daily net profit series from GET /v1/historical/time-patterns."""
    from api_response_transformers import time_patterns_daily_df
    data = fetch_historical_time_patterns(_to_date_only(start_date), _to_date_only(end_date))
    if not data:
        return pd.DataFrame()
    return time_patterns_daily_df(data)


def fetch_canonical_pnl_totals(start_date: str, end_date: str) -> dict:
    """Canonical company P&L totals for a date window from GET /v1/historical/time-patterns.

    Same source as the daily net-profit graph (net_sales - net_cogs - total_ad_spend),
    so WTD/MTD headline figures agree with the daily report. Returns {} on failure.
    """
    try:
        df = fetch_net_profit_series_from_api(start_date, end_date)
        if df is None or df.empty:
            return {}
        return {
            "revenue": float(df["revenue"].sum()),
            "cogs": float(df["cogs"].sum()),
            "ad_spend": float(df["total_ad_spend"].sum()),
            "net_profit": float(df["net_profit"].sum()),
        }
    except Exception as e:
        logger.warning(
            "fetch_canonical_pnl_totals failed (%s -> %s): %s", start_date, end_date, e
        )
        return {}


def fetch_shopify_sales_by_region_api(start_date: str, end_date: str) -> pd.DataFrame:
    """Regional sales from GET /v1/historical/sales-by-region."""
    from api_response_transformers import sales_by_region_to_state_df
    data = fetch_historical_sales_by_region(_to_date_only(start_date), _to_date_only(end_date))
    if not data:
        return pd.DataFrame()
    return sales_by_region_to_state_df(data)


def fetch_shopify_sales_by_state(start_date: str, end_date: str) -> pd.DataFrame:
    """Aggregated sales and order count by state for plotting (API-first)."""
    if not USE_API_ONLY:
        try:
            df = fetch_shopify_sales_by_region_api(start_date, end_date)
            if df is not None and not df.empty:
                return df
        except Exception as e:
            if USE_API_ONLY:
                raise
            logger.warning("sales-by-region API failed (%s); falling back to Postgres", e)
    elif USE_API_ONLY:
        df = fetch_shopify_sales_by_region_api(start_date, end_date)
        return df if df is not None else pd.DataFrame()

    try:
        engine = get_db_engine()
        query = text("""
            SELECT
                COALESCE(NULLIF(TRIM(ship_province), ''), 'Unknown') AS state,
                SUM(o.total_price_amount) AS total_sales,
                COUNT(*)::bigint AS order_count
            FROM public.shopify_orders o
            WHERE o.cancelled_at_ist IS NULL
              AND DATE(processed_at_ist) BETWEEN :start_date AND :end_date
            GROUP BY 1
            ORDER BY total_sales DESC
        """)
        df = pd.read_sql(
            query, engine, params={"start_date": start_date, "end_date": end_date}
        )
        if not df.empty and 'total_sales' in df.columns:
            df = df.copy()
            df['total_sales'] = apply_net_revenue_column(df['total_sales'])
        return df
    except Exception as e:
        logger.warning("fetch_shopify_sales_by_state: %s", e)
        return pd.DataFrame(columns=["state", "total_sales", "order_count"])


# Run-scoped snapshot cache so every report section (entity xlsx, PDF channel
# table, email KPIs) reads the SAME attribution snapshot for a given date range.
# Live "today" data is still being attributed, so independent fetches minutes
# apart would otherwise return different numbers and fail to reconcile.
_MARKETING_HOURLY_CACHE: dict = {}


def clear_marketing_cache() -> None:
    """Drop the run-scoped marketing snapshot cache (e.g. between report runs)."""
    _MARKETING_HOURLY_CACHE.clear()


def fetch_marketing_hourly(start_date: str, end_date: str) -> pd.DataFrame:
    """
    Fetch channel-wise hourly/daily marketing insights for the given date range.

    Primary source: Node-Backend v1 attribution APIs (meta, google, organic).
    Fallback: PostgreSQL dw_*_attribution union when USE_API_ONLY is false.

    Memoized per (start, end) for the process lifetime so all report sections
    share one consistent snapshot (see _MARKETING_HOURLY_CACHE).
    """
    cache_key = (str(start_date)[:10], str(end_date)[:10])
    cached = _MARKETING_HOURLY_CACHE.get(cache_key)
    if cached is not None:
        return cached.copy()

    try:
        df = fetch_marketing_from_api(start_date, end_date)
        if df is not None and not df.empty:
            logger.info("[marketing] API attribution: %d rows for %s to %s", len(df), start_date, end_date)
            _MARKETING_HOURLY_CACHE[cache_key] = df.copy()
            return df
    except Exception as e:
        if USE_API_ONLY:
            raise
        logger.warning("[marketing] API fetch failed (%s); falling back to Postgres", e)

    if USE_API_ONLY:
        logger.error("[marketing] API-only mode: no attribution data for %s to %s", start_date, end_date)
        return pd.DataFrame()

    try:
        engine = get_db_engine()
        sql = (
            """
            WITH meta_data AS (
                SELECT 
                    'Meta Ads' as source,
                    channel,
                    attribution_source,
                    date_start,
                    hour,
                    campaign_id,
                    campaign_name,
                    adset_id,
                    adset_name,
                    ad_id,
                    ad_name,
                    COALESCE(impressions, 0) as impressions,
                    COALESCE(clicks, 0) as clicks,
                    COALESCE(spend, 0.0) as spend_cost,
                    COALESCE(cpm, 0.0) as cpm,
                    COALESCE(cpc, 0.0) as cpc,
                    COALESCE(ctr, 0.0) as ctr,
                    COALESCE(action_onsite_web_view_content, 0) as action_onsite_web_view_content,
                    COALESCE(action_onsite_web_add_to_cart, 0) as action_onsite_web_add_to_cart,
                    COALESCE(action_onsite_web_initiate_checkout, 0) as action_onsite_web_initiate_checkout,
                    COALESCE(action_offsite_pixel_view_content, 0) as action_offsite_pixel_view_content,
                    COALESCE(action_offsite_pixel_add_to_cart, 0) as action_offsite_pixel_add_to_cart,
                    COALESCE(action_offsite_pixel_initiate_checkout, 0) as action_offsite_pixel_initiate_checkout,
                    COALESCE(action_landing_page_view, 0) as action_landing_page_view,
                    COALESCE(attributed_orders_count, 0) as attributed_orders_count,
                    COALESCE(attributed_orders_revenue, 0.0) as attributed_orders_revenue,
                    COALESCE(attributed_orders_cogs, 0.0) as attributed_orders_cogs,
                    COALESCE(attributed_orders_quantity, 0) as attributed_orders_quantity,
                    attributed_orders,
                    product_details,
                    utm_source,
                    utm_medium,
                    utm_campaign,
                    utm_content,
                    utm_term
                FROM public.dw_meta_ads_attribution
                WHERE date_start BETWEEN DATE %(start_date)s AND DATE %(end_date)s
            ),
            google_data AS (
                SELECT 
                    'Google Ads' as source,
                    channel,
                    attribution_source,
                    date_start,
                    hour,
                    campaign_id,
                    campaign_name,
                    CAST(NULL as bigint) as adset_id,
                    CAST(NULL as text) as adset_name,
                    CAST(NULL as bigint) as ad_id,
                    CAST(NULL as text) as ad_name,
                    COALESCE(impressions, 0) as impressions,
                    COALESCE(clicks, 0) as clicks,
                    COALESCE(cost_amount, 0.0) as spend_cost,
                    COALESCE(average_cpm, 0.0) as cpm,
                    COALESCE(average_cpc, 0.0) as cpc,
                    COALESCE(ctr, 0.0) as ctr,
                    0 as action_onsite_web_view_content,
                    0 as action_onsite_web_add_to_cart,
                    0 as action_onsite_web_initiate_checkout,
                    0 as action_offsite_pixel_view_content,
                    0 as action_offsite_pixel_add_to_cart,
                    0 as action_offsite_pixel_initiate_checkout,
                    0 as action_landing_page_view,
                    COALESCE(attributed_orders_count, 0) as attributed_orders_count,
                    COALESCE(attributed_orders_revenue, 0.0) as attributed_orders_revenue,
                    COALESCE(attributed_orders_cogs, 0.0) as attributed_orders_cogs,
                    COALESCE(attributed_orders_quantity, 0) as attributed_orders_quantity,
                    attributed_orders,
                    product_details,
                    utm_source,
                    utm_medium,
                    utm_campaign,
                    utm_content,
                    utm_term
                FROM public.dw_google_ads_attribution
                WHERE date_start BETWEEN DATE %(start_date)s AND DATE %(end_date)s
            ),
            organic_data AS (
                SELECT 
                    'Organic' as source,
                    channel,
                    attribution_source,
                    date_start,
                    hour,
                    CAST(NULL as bigint) as campaign_id,
                    utm_campaign as campaign_name,
                    CAST(NULL as bigint) as adset_id,
                    utm_content as adset_name,
                    CAST(NULL as bigint) as ad_id,
                    utm_term as ad_name,
                    COALESCE(estimated_impressions, 0) as impressions,
                    COALESCE(estimated_clicks, 0) as clicks,
                    COALESCE(estimated_spend, 0.0) as spend_cost,
                    0.0 as cpm,
                    0.0 as cpc,
                    0.0 as ctr,
                    0 as action_onsite_web_view_content,
                    0 as action_onsite_web_add_to_cart,
                    0 as action_onsite_web_initiate_checkout,
                    0 as action_offsite_pixel_view_content,
                    0 as action_offsite_pixel_add_to_cart,
                    0 as action_offsite_pixel_initiate_checkout,
                    0 as action_landing_page_view,
                    COALESCE(attributed_orders_count, 0) as attributed_orders_count,
                    COALESCE(attributed_orders_revenue, 0.0) as attributed_orders_revenue,
                    COALESCE(attributed_orders_cogs, 0.0) as attributed_orders_cogs,
                    COALESCE(attributed_orders_quantity, 0) as attributed_orders_quantity,
                    attributed_orders,
                    product_details,
                    utm_source,
                    utm_medium,
                    utm_campaign,
                    utm_content,
                    utm_term
                FROM public.dw_organic_attribution
                WHERE date_start BETWEEN DATE %(start_date)s AND DATE %(end_date)s
            )
            SELECT * FROM meta_data
            UNION ALL
            SELECT * FROM google_data
            UNION ALL
            SELECT * FROM organic_data
            ORDER BY date_start, hour, source, channel
            """
        )
        df = pd.read_sql(sql, engine, params={
            'start_date': start_date,
            'end_date': end_date,
        })

        if df is not None and not df.empty:
            _MARKETING_HOURLY_CACHE[cache_key] = df.copy()
        return df
    except Exception as e:
        print(f"Database error in fetch_marketing_hourly: {str(e)}")
        return pd.DataFrame()



# =============================================================================
# Node-Backend v1 API client
# =============================================================================

USE_API_ONLY = os.getenv("USE_API_ONLY", "false").lower() in ("1", "true", "yes")
USE_API_FALLBACK = os.getenv("USE_API_FALLBACK", "true").lower() in ("1", "true", "yes")


def _prefer_api() -> bool:
    return USE_API_ONLY or USE_API_FALLBACK


def get_api_brand_id() -> int:
    return int(os.getenv("CLICKHOUSE_BRAND_ID", os.getenv("API_BRAND_ID", "20")))


def get_api_company_id() -> int:
    return int(os.getenv("DASHBOARD_COMPANY_ID", os.getenv("API_COMPANY_ID", "19")))


def _default_tenant_params() -> dict:
    return {"brand_id": get_api_brand_id(), "company_id": get_api_company_id()}


def _to_date_only(value: str) -> str:
    return str(value)[:10]


def pnl_end_exclusive(inclusive_end: str) -> str:
    """PnL routes use exclusive endDate; add one day for inclusive report ranges."""
    d = datetime.strptime(_to_date_only(inclusive_end), "%Y-%m-%d").date()
    return (d + timedelta(days=1)).strftime("%Y-%m-%d")


def fetch_v1(path: str, params: Optional[dict] = None, timeout: int = 120) -> Optional[dict]:
    """
    GET {BASE_URL}/v1/{path} and return the inner `data` object, or None on failure.
    Path should not include a leading slash or the /v1 prefix.
    """
    url = f"{BASE_URL.rstrip('/')}/v1/{path.lstrip('/')}"
    merged = {**_default_tenant_params(), **(params or {})}
    try:
        resp = make_authenticated_request("GET", url, params=merged, timeout=timeout)
        if resp.status_code != 200:
            logger.warning("[v1] %s -> HTTP %s: %s", url, resp.status_code, resp.text[:300])
            return None
        payload = resp.json()
        if isinstance(payload, dict) and payload.get("success") is False:
            logger.warning("[v1] %s -> success=false: %s", url, payload.get("error"))
            return None
        if isinstance(payload, dict) and "data" in payload:
            return payload.get("data")
        return payload if isinstance(payload, dict) else None
    except Exception as e:
        logger.warning("[v1] %s failed: %s", url, e)
        return None


def fetch_historical_dashboard(start_date: str, end_date: str) -> Optional[dict]:
    return fetch_v1("historical/dashboard", {
        "start_date": _to_date_only(start_date),
        "end_date": _to_date_only(end_date),
    })


def fetch_historical_time_patterns(start_date: str, end_date: str) -> Optional[dict]:
    return fetch_v1("historical/time-patterns", {
        "start_date": _to_date_only(start_date),
        "end_date": _to_date_only(end_date),
    })


def fetch_historical_sales_by_region(start_date: str, end_date: str) -> Optional[dict]:
    return fetch_v1("historical/sales-by-region", {
        "start_date": _to_date_only(start_date),
        "end_date": _to_date_only(end_date),
    })


def fetch_historical_meta_ads(start_date: str, end_date: str) -> Optional[dict]:
    return fetch_v1("historical/meta/ads", {
        "start_date": _to_date_only(start_date),
        "end_date": _to_date_only(end_date),
    })


def fetch_historical_google_ads(start_date: str, end_date: str) -> Optional[dict]:
    return fetch_v1("historical/google/ads", {
        "start_date": _to_date_only(start_date),
        "end_date": _to_date_only(end_date),
    })


def fetch_historical_amazon_dashboard(start_date: str, end_date: str) -> Optional[dict]:
    return fetch_v1("historical/amazon/dashboard", {
        "start_date": _to_date_only(start_date),
        "end_date": _to_date_only(end_date),
    })


def fetch_historical_amazon_ads(start_date: str, end_date: str) -> Optional[dict]:
    return fetch_v1("historical/amazon/ads", {
        "start_date": _to_date_only(start_date),
        "end_date": _to_date_only(end_date),
    })


def fetch_historical_amazon_sp_sales(start_date: str, end_date: str) -> Optional[dict]:
    return fetch_v1("historical/amazon-sp-sales", {
        "start_date": _to_date_only(start_date),
        "end_date": _to_date_only(end_date),
    })


def fetch_meta_attribution(
    start_date: str,
    end_date: str,
    time_aggregation: Optional[str] = None,
) -> Optional[dict]:
    params = {
        "start_date": _to_date_only(start_date),
        "end_date": _to_date_only(end_date),
    }
    if time_aggregation:
        params["time_aggregation"] = time_aggregation
    return fetch_v1("meta-attribution", params)


def fetch_google_attribution(
    start_date: str,
    end_date: str,
    time_aggregation: Optional[str] = None,
) -> Optional[dict]:
    params = {
        "start_date": _to_date_only(start_date),
        "end_date": _to_date_only(end_date),
    }
    if time_aggregation:
        params["time_aggregation"] = time_aggregation
    return fetch_v1("google-attribution", params)


def fetch_channel_attribution(
    start_date: str,
    end_date: str,
    channel: str = "organic",
    time_aggregation: Optional[str] = None,
) -> Optional[dict]:
    params = {
        "start_date": _to_date_only(start_date),
        "end_date": _to_date_only(end_date),
        "channel": channel,
    }
    if time_aggregation:
        params["time_aggregation"] = time_aggregation
    return fetch_v1("channel-attribution", params)


def fetch_amazon_attribution(start_date: str, end_date: str) -> Optional[dict]:
    return fetch_v1("amazon-attribution", {
        "start_date": _to_date_only(start_date),
        "end_date": _to_date_only(end_date),
    })


def fetch_meta_funnel(start_date: str, end_date: str) -> Optional[dict]:
    return fetch_v1("meta-funnel", {
        "start_date": _to_date_only(start_date),
        "end_date": _to_date_only(end_date),
    })


def fetch_channel_funnel(
    start_date: str,
    end_date: str,
    channel: str = "meta",
) -> Optional[dict]:
    """Unified per-channel on-site session funnel from GET /v1/funnel.

    channel: 'meta' | 'google' | 'organic'. Returns the inner data object with
    keys: period, channel, label, is_paid, funnel, performance, breakdowns,
    top_products. `funnel` carries the session-based stages (sessions ->
    product_view -> add_to_cart -> checkout -> converted); `performance`
    carries ad-delivery metrics (spend, impressions, clicks, attributed_orders).
    """
    return fetch_v1("funnel", {
        "start_date": _to_date_only(start_date),
        "end_date": _to_date_only(end_date),
        "channel": channel,
    })


def fetch_pnl_summary(start_date: str, end_date: str) -> Optional[dict]:
    return fetch_v1("pnl/summary", {
        "startDate": _to_date_only(start_date),
        "endDate": pnl_end_exclusive(end_date),
    })


def fetch_marketing_from_api(start_date: str, end_date: str) -> pd.DataFrame:
    """Fetch attribution data from v1 APIs and flatten to marketing-hourly DataFrame."""
    from api_response_transformers import flatten_all_attribution_to_hourly_df

    start_s, end_s = _to_date_only(start_date), _to_date_only(end_date)
    return flatten_all_attribution_to_hourly_df(
        start_s,
        end_s,
        fetch_meta=lambda s, e, **kw: fetch_meta_attribution(s, e, kw.get("time_aggregation")),
        fetch_google=lambda s, e, **kw: fetch_google_attribution(s, e, kw.get("time_aggregation")),
        fetch_organic=lambda s, e, **kw: fetch_channel_attribution(s, e, kw.get("channel", "organic")),
    )


def get_organized_metrics_for_pdf(timeframe_start=None, timeframe_end=None):
    """
    Build organized metrics for PDF summary from GET /v1/historical/dashboard
    (dashboard-aligned formulas via metric_calculators).
    """
    from metric_calculators import channel_metrics_from_historical_dashboard

    try:
        if timeframe_start is not None and timeframe_end is not None:
            ist = pytz.timezone("Asia/Kolkata")
            start_ist = (
                timeframe_start.astimezone(ist)
                if getattr(timeframe_start, "tzinfo", None)
                else ist.localize(timeframe_start)
            )
            end_ist = (
                timeframe_end.astimezone(ist)
                if getattr(timeframe_end, "tzinfo", None)
                else ist.localize(timeframe_end)
            )
            start_str = start_ist.strftime("%Y-%m-%d")
            end_str = end_ist.strftime("%Y-%m-%d")
        else:
            start_str, end_str = get_today_ist_hour_bounds()
            start_str = start_str[:10]
            end_str = end_str[:10]
    except Exception:
        start_str, end_str = get_today_ist_hour_bounds()
        start_str = start_str[:10]
        end_str = end_str[:10]

    data = fetch_historical_dashboard(start_str, end_str)
    if data:
        result = channel_metrics_from_historical_dashboard(data)
        return {k: result[k] for k in ("meta", "google", "organic", "total") if k in result}

    if USE_API_ONLY:
        logger.error("[PDF metrics] API-only mode: historical/dashboard returned no data")
        empty = {
            "sales": 0, "ad_spend": 0, "cogs": 0, "net_profit": 0,
            "gross_roas": 0, "net_roas": 0, "be_roas": 0,
            "quantity": 0, "cpp": 0, "order_count": 0,
        }
        return {"meta": empty, "google": dict(empty), "organic": dict(empty), "total": dict(empty)}

    # Legacy fallback (deprecated — endpoints may not exist on Node-Backend)
    start_dt_str, end_dt_str = get_today_ist_hour_bounds()
    ad_spend_json = fetch_ad_spend_by_hour(start_dt_str, end_dt_str) or {}
    sales_json = fetch_sales_unitcost_by_hour(start_dt_str, end_dt_str) or {}
    totals_spend = (ad_spend_json or {}).get('totals', {}) or {}
    sum_sales = (sales_json or {}).get('sum', {}) or {}

    facebook_spend = float(totals_spend.get('facebookSpend', 0) or 0)
    google_spend = float(totals_spend.get('googleSpend', 0) or 0)
    total_spend = facebook_spend + google_spend

    meta_sales = float(sum_sales.get('meta_sales', 0) or 0)
    google_sales = float(sum_sales.get('google_sales', 0) or 0)
    organic_sales = float(sum_sales.get('organic_sales', 0) or 0)
    total_sales = float(sum_sales.get('total_sales', 0) or 0)

    total_unit_cost = float(sum_sales.get('total_unit_cost', 0) or 0)
    unit_cost_meta = float(sum_sales.get('unit_cost_meta', 0) or 0)
    unit_cost_google = float(sum_sales.get('unit_cost_google', 0) or 0)
    unit_cost_organic = float(sum_sales.get('unit_cost_organic', 0) or 0)

    total_orders = int(sum_sales.get('order_count', 0) or 0)
    meta_orders = int(sum_sales.get('meta_order_count', 0) or 0)
    google_orders = int(sum_sales.get('google_order_count', 0) or 0)
    organic_orders = int(sum_sales.get('organic_order_count', 0) or 0)

    # Helpers
    def safe_div(n, d):
        try:
            n = float(n)
            d = float(d)
            return (n / d) if d else 0.0
        except Exception:
            return 0.0

    # Meta bucket
    meta_ad_spend = facebook_spend
    meta_cogs = unit_cost_meta
    meta_net_profit = meta_sales - meta_cogs - meta_ad_spend
    meta = {
        'sales': meta_sales,
        'ad_spend': meta_ad_spend,
        'cogs': meta_cogs,
        'net_profit': meta_net_profit,
        'gross_roas': round(safe_div(meta_sales, meta_ad_spend), 2),
        'net_roas': round(safe_div(meta_sales - meta_cogs, meta_ad_spend), 2),
        'be_roas': round(safe_div(meta_cogs + meta_ad_spend, meta_ad_spend), 2),
        'quantity': meta_orders,
        'cpp': round(safe_div(meta_ad_spend, meta_orders), 2),
        'order_count': meta_orders,
    }

    # Google bucket
    google_ad_spend = google_spend
    google_cogs = unit_cost_google
    google_net_profit = google_sales - google_cogs - google_ad_spend
    google = {
        'sales': google_sales,
        'ad_spend': google_ad_spend,
        'cogs': google_cogs,
        'net_profit': google_net_profit,
        'gross_roas': round(safe_div(google_sales, google_ad_spend), 2),
        'net_roas': round(safe_div(google_sales - google_cogs, google_ad_spend), 2),
        'be_roas': round(safe_div(google_cogs + google_ad_spend, google_ad_spend), 2),
        'quantity': google_orders,
        'cpp': round(safe_div(google_ad_spend, google_orders), 2),
        'order_count': google_orders,
    }

    # Organic bucket (no spend)
    organic_cogs = unit_cost_organic
    organic_net_profit = organic_sales - organic_cogs
    organic = {
        'sales': organic_sales,
        'ad_spend': 0,
        'cogs': organic_cogs,
        'net_profit': organic_net_profit,
        'gross_roas': 0,
        'net_roas': 0,
        'be_roas': 0,
        'quantity': organic_orders,
        'cpp': 0,
        'order_count': organic_orders,
    }

    # Total bucket
    total_net_profit = total_sales - total_unit_cost - total_spend
    total = {
        'sales': total_sales,
        'ad_spend': total_spend,
        'cogs': total_unit_cost,
        'net_profit': total_net_profit,
        'gross_roas': round(safe_div(total_sales, total_spend), 2),
        'net_roas': round(safe_div(total_sales - total_unit_cost, total_spend), 2),
        'be_roas': round(safe_div(total_unit_cost + total_spend, total_spend), 2),
        'quantity': total_orders,
        'cpp': round(safe_div(total_spend, total_orders), 2),
        'order_count': total_orders,
    }

    return {
        'meta': meta,
        'google': google,
        'organic': organic,
        'total': total,
    }


def fetch_entity_mapping(start_date: str | None = None, end_date: str | None = None):
    """
    Call the external entity-mapping endpoint.

    Params must be in 'YYYY-MM-DD HH' format. When not provided, defaults to today's
    IST range: 'YYYY-MM-DD 00' to 'YYYY-MM-DD 23'.
    """
    try:
        if not start_date or not end_date:
            tf = get_timeframe_config()
            start_date = tf['start_date'].strftime('%Y-%m-%d %H')
            end_date = tf['end_date'].strftime('%Y-%m-%d %H')
        url = f"{SKU_SPEND_SALES_BASE}/entity-mapping"
        resp = requests.get(url, params={
            'start_date': start_date,
            'end_date': end_date,
        })
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"Error fetching entity mapping: {e}")
        return {}


def fetch_product_profitability(start_datetime: str | None = None, end_datetime: str | None = None):
    """
    Call the external product spend/meta endpoint (product profitability by SKU).

    Params can be in 'YYYY-MM-DD' or 'YYYY-MM-DD HH' format; only the date part is sent.
    When not provided, defaults to today's IST date range.
    Returns: {"products": [...], "summary": {...}} or {} on error.
    """
    try:
        if not start_datetime or not end_datetime:
            tf = get_timeframe_config()
            start_datetime = tf['start_date'].strftime('%Y-%m-%d')
            end_datetime = tf['end_date'].strftime('%Y-%m-%d')
        # API expects date-only params (YYYY-MM-DD)
        start_date = start_datetime.strip()[:10]
        end_date = end_datetime.strip()[:10]
        url = f"{SKU_SPEND_SALES_BASE}/product_spend/all"
        resp = requests.get(url, params={
            'start_datetime': start_date,
            'end_datetime': end_date,
        })
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            data = dict(data)
            products = data.get('products')
            if isinstance(products, list):
                for p in products:
                    if isinstance(p, dict) and 'revenue' in p:
                        p['revenue'] = apply_net_revenue(float(p.get('revenue', 0) or 0))
        return data
    except Exception as e:
        print(f"Error fetching product profitability: {e}")
        return {}


def _normalize_amazon_ads_df(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize API Amazon ads DataFrame to legacy PG column names where possible."""
    out = df.copy()
    if "report_date" in out.columns and "date" not in out.columns:
        out["date"] = out["report_date"]
    if "spend" in out.columns:
        out["ctr"] = out.apply(
            lambda r: (float(r.get("clicks", 0) or 0) / float(r["impressions"]) * 100)
            if float(r.get("impressions", 0) or 0) > 0 else 0.0,
            axis=1,
        )
        out["cpc"] = out.apply(
            lambda r: float(r.get("spend", 0) or 0) / float(r["clicks"])
            if float(r.get("clicks", 0) or 0) > 0 else 0.0,
            axis=1,
        )
        out["roas"] = out.apply(
            lambda r: float(r.get("sales", 0) or 0) / float(r["spend"])
            if float(r.get("spend", 0) or 0) > 0 else 0.0,
            axis=1,
        )
        out["acos"] = out.apply(
            lambda r: float(r.get("spend", 0) or 0) / float(r["sales"]) * 100
            if float(r.get("sales", 0) or 0) > 0 else 0.0,
            axis=1,
        )
    return out


def fetch_amazon_data(start_date: str | None = None, end_date: str | None = None):
    """
    Fetch Amazon Ads campaign metrics for a given date range.

    Primary: GET /v1/historical/amazon/ads or /v1/amazon-attribution.
    Fallback: PostgreSQL amazon_product_metrics_daily when API unavailable.
    """
    tf = get_timeframe_config(start_date=start_date, end_date=end_date)
    start_str = tf['start_date'].strftime('%Y-%m-%d')
    end_str = tf['end_date'].strftime('%Y-%m-%d')

    if _prefer_api():
        try:
            from api_response_transformers import amazon_ads_daily_from_attribution, amazon_ads_from_historical
            attr = fetch_amazon_attribution(start_str, end_str)
            if attr:
                df = amazon_ads_daily_from_attribution(attr)
                if not df.empty:
                    print(f"[Amazon API] {len(df)} rows from amazon-attribution for {start_str} to {end_str}")
                    return _normalize_amazon_ads_df(df)
            hist = fetch_historical_amazon_ads(start_str, end_str)
            if hist:
                df = amazon_ads_from_historical(hist)
                if not df.empty:
                    print(f"[Amazon API] {len(df)} rows from historical/amazon/ads for {start_str} to {end_str}")
                    return _normalize_amazon_ads_df(df)
        except Exception as e:
            if USE_API_ONLY:
                raise
            print(f"[Amazon API] fetch failed ({e}); falling back to Postgres")

    if USE_API_ONLY:
        print(f"[Amazon] API-only mode: no data for {start_str} to {end_str}")
        return pd.DataFrame()

    try:
        engine = get_db_engine()
        query = """
            SELECT 
                id,
                campaign_id,
                campaign_name,
                adgroup_id,
                adgroup_name,
                product_ad_id,
                asin,
                sku,
                date,
                COALESCE(impressions, 0) as impressions,
                COALESCE(clicks, 0) as clicks,
                COALESCE(spend, 0.0) as spend,
                COALESCE(orders, 0) as orders,
                COALESCE(sales, 0.0) as sales,
                COALESCE(ctr, 0.0) as ctr,
                COALESCE(cpc, 0.0) as cpc,
                COALESCE(acos, 0.0) as acos,
                COALESCE(roas, 0.0) as roas,
                created_at,
                updated_at
            FROM public.amazon_product_metrics_daily
            WHERE date BETWEEN DATE %(start_date)s AND DATE %(end_date)s
            ORDER BY date, campaign_id, adgroup_id, asin
        """
        
        print(f"[Amazon] Fetching Amazon product metrics for date range: {start_str} to {end_str}")
        
        df = pd.read_sql(query, engine, params={
            'start_date': start_str,
            'end_date': end_str,
        })
        
        # Debug: Show what data we got
        if not df.empty:
            unique_dates = sorted(df['date'].unique()) if 'date' in df.columns else []
            unique_campaigns = df['campaign_name'].nunique() if 'campaign_name' in df.columns else 0
            total_spend = df['spend'].sum() if 'spend' in df.columns else 0
            total_sales = df['sales'].sum() if 'sales' in df.columns else 0
            print(f"[Amazon] Found {len(df)} records for {len(unique_dates)} unique dates")
            print(f"[Amazon] {unique_campaigns} unique campaigns, Total Spend: ₹{total_spend:.2f}, Total Sales: ₹{total_sales:.2f}")
        else:
            print(f"[Amazon] No data found for date range {start_str} to {end_str}")
        
        return df
    except Exception as e:
        print(f"Database error in fetch_amazon_data: {str(e)}")
        return pd.DataFrame()