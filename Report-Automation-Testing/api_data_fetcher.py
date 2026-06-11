import requests
import pandas as pd
from sqlalchemy import create_engine, text
from database_manager import get_db_engine
from datetime import datetime, timedelta
import os  
import pytz
import json
from typing import Optional, Dict
from timeframe_config import get_timeframe_config
import logging
from revenue_gst import apply_net_revenue, apply_net_revenue_column, adjust_net_profit_single_day_payload

logger = logging.getLogger(__name__)  # Use module logger for diagnostic messages

BASE_URL = os.getenv('BACKEND_API_BASE_URL', "https://node.seleric.com/api").strip()

# External SKU/Spend/Sales reporting service base
SKU_SPEND_SALES_BASE = os.getenv('SKU_SPEND_SALES_BASE', "https://skuspendsales-aghtewckaqbdfqep.centralindia-01.azurewebsites.net/api").strip()

"""Authentication helpers for BASE_URL requests (Firebase ID token).

Authentication Methods (in priority order):
1. Direct token: Set FIREBASE_ID_TOKEN environment variable with a Firebase user ID token
2. Auto-generation: Automatically generates tokens using email/password credentials
   - Uses FIREBASE_WEB_API_KEY, FIREBASE_EMAIL, FIREBASE_PASSWORD (must be set in environment)
   - Tokens are cached and auto-refreshed on expiration

All requests to BASE_URL will automatically include the Authorization header when a token is available.
Tokens are automatically refreshed on 401 errors.
"""
API_BEARER_TOKEN = os.getenv('FIREBASE_ID_TOKEN', '').strip()
FIREBASE_TOKEN_CACHE = {'token': None, 'expires_at': None}

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

def get_firebase_token() -> Optional[str]:
    """Get a Firebase token, generating it if needed.
    
    Priority order:
    1. Direct token from FIREBASE_ID_TOKEN environment variable
    2. Auto-generated token using email/password credentials from environment
    
    Returns:
        Token string or None
    """
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
    """Clear the cached token (useful when token is invalid/expired)."""
    global FIREBASE_TOKEN_CACHE
    FIREBASE_TOKEN_CACHE['token'] = None
    FIREBASE_TOKEN_CACHE['expires_at'] = None
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
                
            # Handle 401 Unauthorized
            if response.status_code == 401 and retry_on_401 and attempt < max_retries:
                logger.warning(f"[API] 401 Unauthorized on attempt {attempt+1}. Clearing token cache and retrying...")
                clear_token_cache()
                attempt += 1
                import time
                time.sleep(1) # Small delay before retry
                continue
            
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
    """
    try:
        tf = get_timeframe_config(start_date=start_date, end_date=end_date)
        start_str = tf['start_date'].strftime('%Y-%m-%d')
        end_str = tf['end_date'].strftime('%Y-%m-%d')
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


def fetch_shopify_sales_by_state(start_date: str, end_date: str) -> pd.DataFrame:
    """Aggregated sales and order count by state (ship_province) for plotting."""
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


def fetch_marketing_hourly(start_date: str, end_date: str) -> pd.DataFrame:
    """
    Fetch channel-wise hourly marketing insights from attribution tables for the given date range.

    Args:
        start_date: Inclusive start date in 'YYYY-MM-DD'.
        end_date: Inclusive end date in 'YYYY-MM-DD'.

    Returns:
        pandas.DataFrame containing channel-wise data from dw_meta_ads_attribution,
        dw_google_ads_attribution, and dw_organic_attribution tables.
    """
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
            
        return df
    except Exception as e:
        print(f"Database error in fetch_marketing_hourly: {str(e)}")
        return pd.DataFrame()

def get_organized_metrics_for_pdf(timeframe_start=None, timeframe_end=None):
    """
    Build organized metrics for PDF summary using new hourly backend endpoints:
    - ad_spend_by_hour
    - sales_unitCost_by_hour

    If timeframe_start/end are provided, they should be datetime-like objects. We'll
    convert them to IST and format as 'YYYY-MM-DD HH'. Otherwise, defaults to today's IST day range.
    """
    # Compute timeframe strings in 'YYYY-MM-DD HH'
    try:
        if timeframe_start is not None and timeframe_end is not None:
            ist = pytz.timezone('Asia/Kolkata')
            start_ist = timeframe_start.astimezone(ist) if getattr(timeframe_start, 'tzinfo', None) else ist.localize(timeframe_start)
            end_ist = timeframe_end.astimezone(ist) if getattr(timeframe_end, 'tzinfo', None) else ist.localize(timeframe_end)
            start_dt_str = start_ist.strftime('%Y-%m-%d %H')
            end_dt_str = end_ist.strftime('%Y-%m-%d %H')
        else:
            start_dt_str, end_dt_str = get_today_ist_hour_bounds()
    except Exception:
        start_dt_str, end_dt_str = get_today_ist_hour_bounds()

    # Fetch from backend
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



def fetch_amazon_data(start_date: str | None = None, end_date: str | None = None):
    """
    Fetch Amazon product metrics data from the database for a given date range.
    
    Args:
        start_date: Start date in 'YYYY-MM-DD' format. If None, uses timeframe config.
        end_date: End date in 'YYYY-MM-DD' format. If None, uses timeframe config.
    
    Returns:
        pandas.DataFrame containing Amazon product metrics including campaign, adgroup,
        ASIN, SKU, impressions, clicks, spend, orders, sales, CTR, CPC, ACOS, and ROAS.
    """
    try:
        # Get timeframe configuration
        tf = get_timeframe_config(start_date=start_date, end_date=end_date)
        start_str = tf['start_date'].strftime('%Y-%m-%d')
        end_str = tf['end_date'].strftime('%Y-%m-%d')
        
        # Use centralized database engine
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