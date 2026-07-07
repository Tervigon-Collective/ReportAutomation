import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), 'intelligence'))
import numpy as np
import pandas as pd
import re
from sqlalchemy import create_engine
from pathlib import Path
from datetime import datetime , timedelta
import logging
from urllib.parse import urlparse
import requests
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
import time
import pytz
import msal
import base64
import glob
from global_config import get_global_config, get_facebook_ads_config, get_temp_dir, get_report_dir
# Import external functions
from plots import generate_plots_for_email
from sku_matching import match_product_to_sku
from timeframe_config import get_timeframe_config, get_current_timestamp, get_daily_report_timeframe, set_global_dates

from sqlalchemy.sql import text
from googleads import get_google_ads_total_spend, get_th_account_metrics
from metaActivityTrack import generate_meta_activity_excel
from database_manager import get_db_engine, dispose_engine
from ad_recommendations import generate_ad_recommendations_report
from api_data_fetcher import get_organized_metrics_for_pdf
from revenue_gst import apply_net_revenue_column
from dailyrollup import get_campaign_data, run as run_dailyrollup, get_campaign_grand_total_for_pdf, get_meta_funnel_metrics

# Populated by generate_pdf_report; consumed by send_email to render the rich HTML body
_daily_email_context: "dict | None" = None


def get_google_funnel_metrics(start_date=None, end_date=None):
    """
    Get Google Ads funnel metrics from dw_google_ads_attribution table.
    Returns metrics for: Clicks, CTR, Interaction Rate, Orders
    
    Args:
        start_date: Optional start date (datetime or date object). If None, uses timeframe_config.
        end_date: Optional end date (datetime or date object). If None, uses timeframe_config.
    """
    try:
        from timeframe_config import get_timeframe_config
        
        # Get the timeframe configuration - use provided dates or default to today
        if start_date is not None and end_date is not None:
            # Convert to date objects if datetime
            if hasattr(start_date, 'date'):
                start_date = start_date.date()
            if hasattr(end_date, 'date'):
                end_date = end_date.date()
        else:
            # Use timeframe_config if dates not provided
            timeframe = get_timeframe_config(start_date, end_date, days_range=1)
            start_date = timeframe['start_date'].date() if hasattr(timeframe['start_date'], 'date') else timeframe['start_date']
            end_date = timeframe['end_date'].date() if hasattr(timeframe['end_date'], 'date') else timeframe['end_date']
        
        # Use centralized database engine
        engine = get_db_engine()
        
        # First, check if there's any data in the table for the date range
        check_query = """
            SELECT COUNT(*) as row_count
            FROM public.dw_google_ads_attribution
            WHERE date_start BETWEEN %s AND %s
        """
        
        check_result = pd.read_sql(check_query, engine, params=(start_date, end_date))
        row_count = check_result.iloc[0]['row_count'] if not check_result.empty else 0
        
        logger.info(f"Google Ads attribution table has {row_count} rows for date range {start_date} to {end_date}")
        
        if row_count == 0:
            logger.warning("No Google Ads data found in dw_google_ads_attribution table for the specified date range")
            # Try to get data from conversion tracking system as fallback
            try:
                from conversionTracking import get_google_metrics
                conversion_metrics = get_google_metrics()
                
                if conversion_metrics and isinstance(conversion_metrics, dict):
                    logger.info(f"Using conversion tracking Google data as fallback: {conversion_metrics}")
                    return {
                        'clicks': int(conversion_metrics.get('total_clicks', 0)),
                        'ctr': float(conversion_metrics.get('avg_ctr', 0.0)),
                        'interaction_rate': 0.0,  # Not available from conversion tracking
                        'orders': int(conversion_metrics.get('total_orders', 0))
                    }
            except Exception as conversion_error:
                logger.warning(f"Conversion tracking Google data fallback failed: {str(conversion_error)}")
            
            # Try to get data from Google Ads API as final fallback
            try:
                from googleads import get_th_account_metrics
                api_metrics = get_th_account_metrics(start_date.strftime('%Y-%m-%d'))
                
                if api_metrics:
                    logger.info(f"Using Google Ads API data as final fallback: {api_metrics}")
                    return {
                        'clicks': int(api_metrics.get('clicks', 0)),
                        'ctr': float(api_metrics.get('ctr', 0.0)),
                        'interaction_rate': 0.0,  # Not available from API
                        'orders': 0  # Not available from API
                    }
            except Exception as api_error:
                logger.warning(f"Google Ads API fallback also failed: {str(api_error)}")
            
            return {
                'clicks': 0,
                'ctr': 0.0,
                'interaction_rate': 0.0,
                'orders': 0
            }
        
        # Query to get Google funnel metrics (multiply by 100 for percentages)
        query = """
            SELECT 
                SUM(clicks) as total_clicks,
                AVG(ctr) * 100 as avg_ctr,
                AVG(interaction_rate) * 100 as avg_interaction_rate,
                SUM(attributed_orders_count) as total_orders
            FROM public.dw_google_ads_attribution
            WHERE date_start BETWEEN %s AND %s
        """
        
        result = pd.read_sql(query, engine, params=(start_date, end_date))
        
        if result.empty or result.iloc[0]['total_clicks'] is None:
            logger.warning("No Google Ads funnel data found for the specified date range")
            return {
                'clicks': 0,
                'ctr': 0.0,
                'interaction_rate': 0.0,
                'orders': 0
            }
        
        row = result.iloc[0]
        
        google_metrics = {
            'clicks': int(row['total_clicks'] or 0),
            'ctr': float(row['avg_ctr'] or 0.0),
            'interaction_rate': float(row['avg_interaction_rate'] or 0.0),
            'orders': int(row['total_orders'] or 0)
        }
        
        logger.info(f"Google funnel metrics: {google_metrics}")
        return google_metrics
        
    except Exception as e:
        logger.error(f"Error fetching Google funnel metrics from dw_google: {str(e)}")
        return {
            'clicks': 0,
            'ctr': 0.0,
            'interaction_rate': 0.0,
            'orders': 0
        }


# Set up logging
_temp_dir = get_temp_dir()
os.makedirs(_temp_dir, exist_ok=True)  # Ensure temp directory exists
_log_file = os.path.join(_temp_dir, 'export_metrics.log')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(_log_file),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Set timezone to IST
IST = pytz.timezone('Asia/Kolkata')

# Load environment variables from global config
ACCESS_TOKEN = get_global_config('Socialpepper_FB_ACCESS_TOKEN')
ACCOUNT_ID = get_global_config('Socialpepper_FB_AD_ACCOUNT_ID')
REPORT_DIR = get_report_dir()
CLIENT_ID = get_global_config('AZURE_CLIENT_ID')
CLIENT_SECRET = get_global_config('AZURE_CLIENT_SECRET')
TENANT_ID = get_global_config('AZURE_TENANT_ID')

# Load email settings from environment variables
import os
from dotenv import load_dotenv
load_dotenv()
EMAIL_SENDER = os.getenv('EMAIL_SENDER', '')
EMAIL_RECIPIENTS = os.getenv('EMAIL_RECIPIENTS', '').split(',')

# Ensure report directory exists
os.makedirs(REPORT_DIR, exist_ok=True)

# Validate environment variables
required_env_vars = {
    'Socialpepper_FB_ACCESS_TOKEN': ACCESS_TOKEN,
    'Socialpepper_FB_AD_ACCOUNT_ID': ACCOUNT_ID,
    'EMAIL_SENDER': EMAIL_SENDER,
    'EMAIL_RECIPIENTS': EMAIL_RECIPIENTS,
    'AZURE_CLIENT_ID': CLIENT_ID,
    'AZURE_CLIENT_SECRET': CLIENT_SECRET,
    'AZURE_TENANT_ID': TENANT_ID
}
for var_name, var_value in required_env_vars.items():
    if not var_value or (var_name == 'EMAIL_RECIPIENTS' and not var_value[0]):
        logger.error(f"Environment variable {var_name} is missing or invalid.")
        raise ValueError(f"Environment variable {var_name} is missing or invalid.")

# Database configuration from global config
DB_CONFIG = {
    'host': get_global_config('DB_HOST', '72.61.228.168'),
    'port': int(get_global_config('DB_PORT', 5432)),
    'database': get_global_config('DB_NAME', 'seleric_db'),
    'user': get_global_config('DB_USER', 'admin_seleric'),
    'password': get_global_config('DB_PASSWORD', 'SelericDB246'),
    'sslmode': 'require'
}

required_keys = ['host', 'port', 'database', 'user', 'password']
missing_keys = [key for key in required_keys if not DB_CONFIG.get(key)]
if missing_keys:
    logger.error(f"Missing required database configuration: {', '.join(missing_keys)}.")
    DB_CONFIG.setdefault('host', 'localhost')
    DB_CONFIG.setdefault('port', 5432)
    DB_CONFIG.setdefault('database', 'metadb_kid4')
    DB_CONFIG.setdefault('user', 'metadb_kid4_user')
    DB_CONFIG.setdefault('password', 'wMzJ24emNkOwEt7ZJ9dx6DcHpPLtZhbd')

logger.info(f"Database configuration: host={DB_CONFIG['host']}, port={DB_CONFIG['port']}, database={DB_CONFIG['database']}, user={DB_CONFIG['user']}")

# Meta API endpoint
BASE_URL = f'https://graph.facebook.com/v22.0/act_{ACCOUNT_ID}/insights'

def fetch_product_metrics():
    try:
        # Use centralized database engine
        engine = get_db_engine()
        
        # Execute the query
        query = "SELECT sku_name, selling_price, per_bottle_cost, net_margin FROM product_metrics"
        result = pd.read_sql(query, engine)
        return result
    except Exception as e:
        logger.error(f"Database error in fetch_product_metrics: {str(e)}")
        # Re-raise the exception after logging
        raise

def fetch_active_campaigns_with_spend(today):
    """
    Fetch active campaigns from Meta API for the given date with their spend data.
    Returns a dictionary mapping campaign_id to spend amount.
    """
    # Use the provided today parameter or get current date
    if isinstance(today, str):
        today = datetime.strptime(today, "%Y-%m-%d").date()
    elif isinstance(today, datetime):
        today = today.date()
    else:
        today = datetime.now(IST).date()

    # Ensure we're not using a future date
    current_date = datetime.now(IST).date()
    if today > current_date:
        logger.warning(f"Requested date {today} is in the future, using current date {current_date}")
        today = current_date

    start_date = today.strftime('%Y-%m-%d')
    end_date = today.strftime('%Y-%m-%d')

    logger.info(f"Fetching active campaigns with spend for date range {start_date} to {end_date}")

    # Fetch active campaigns at campaign level using exact API format from curl example
    campaign_url = f'https://graph.facebook.com/v19.0/act_{ACCOUNT_ID}/insights'
    campaign_params = {
        'access_token': ACCESS_TOKEN,
        'fields': 'campaign_id,campaign_name,spend,ctr,impressions,clicks',
        'level': 'campaign',
        'time_range': f'{{"since":"{start_date}","until":"{end_date}"}}',
        'limit': 1000
    }
    
    campaign_spend_data = {}
    retries = 3
    
    for attempt in range(retries):
            try:
                # Log the request details for debugging
                logger.info(f"Making API request to: {campaign_url}")
                logger.info(f"Parameters: {campaign_params}")
                
                response = requests.get(campaign_url, params=campaign_params)
                
                # Log response details
                logger.info(f"Response status: {response.status_code}")
                logger.info(f"Response URL: {response.url}")
                
                if response.status_code == 403:
                    logger.error(f"403 Forbidden error. This might be due to:")
                    logger.error("1. Invalid access token")
                    logger.error("2. Insufficient permissions")
                    logger.error("3. Account ID mismatch")
                    logger.error("4. Token expiration")
                    logger.warning("Returning empty data due to 403 error")
                    return {}
                
                response.raise_for_status()
                json_response = response.json()
                
                if 'data' not in json_response:
                    logger.error(f"Unexpected API response: {json_response}")
                    raise Exception("Unexpected API response: Missing 'data' key")
                
                # Extract all campaigns from the response with their spend data
                for campaign in json_response.get('data', []):
                    campaign_id = campaign.get('campaign_id')
                    campaign_name = campaign.get('campaign_name')
                    spend = float(campaign.get('spend', 0))
                    ctr = float(campaign.get('ctr', 0))
                    impressions = int(campaign.get('impressions', 0))
                    clicks = int(campaign.get('clicks', 0))
                    
                    if campaign_id:
                        campaign_spend_data[campaign_id] = {
                            'campaign_name': campaign_name,
                            'spend': spend,
                            'ctr': ctr,
                            'impressions': impressions,
                            'clicks': clicks
                    }
                        logger.debug(f"Found active campaign: {campaign_name} (ID: {campaign_id}, Spend: {spend}, CTR: {ctr}%)")
                
                logger.info(f"Found {len(campaign_spend_data)} active campaigns with spend and CTR data from Meta API")
                return campaign_spend_data
                
            except requests.exceptions.HTTPError as e:
                if response.status_code == 429:
                    logger.warning(f"Rate limit hit, retrying in {2 ** attempt} seconds...")
                    time.sleep(2 ** attempt)
                    continue
                elif response.status_code == 400:
                    logger.error(f"Bad request error: {str(e)}")
                    logger.warning("Meta API returned 400 error, returning empty data")
                    return {}
                elif response.status_code == 403:
                    logger.error(f"403 Forbidden error on attempt {attempt + 1}: {str(e)}")
                    if attempt == retries - 1:  # Last attempt
                        logger.warning("Returning empty data due to persistent 403 error")
                        return {}
                    continue
                logger.error(f"API request failed: {str(e)}")
                if attempt == retries - 1:  # Last attempt
                    raise Exception(f"API request failed: {str(e)}")
                continue
            except requests.exceptions.RequestException as e:
                logger.error(f"API request failed: {str(e)}")
                if attempt == retries - 1:  # Last attempt
                    raise Exception(f"API request failed: {str(e)}")
                continue
        
    return {}

def fetch_active_campaigns(today):
    """
    Fetch only active campaigns from Meta API for the given date.
    Returns a set of active campaign IDs.
    """
    campaign_spend_data = fetch_active_campaigns_with_spend(today)
    return set(campaign_spend_data.keys())

def fetch_meta_data(today):
    # Use the provided today parameter or get current date
    if isinstance(today, str):
        today = datetime.strptime(today, "%Y-%m-%d").date()
    elif isinstance(today, datetime):
        today = today.date()
    else:
        today = datetime.now(IST).date()

    # Ensure we're not using a future date
    current_date = datetime.now(IST).date()
    if today > current_date:
        logger.warning(f"Requested date {today} is in the future, using current date {current_date}")
        today = current_date

    start_date = (today - timedelta(days=0)).isoformat()
    end_date = today.isoformat()

    logger.info(f"Using date range {start_date} to {end_date}")

    # Temporarily disabled — active-campaign fetch is unused (filtering never applied below).
    # active_campaigns = fetch_active_campaigns(today)
    # logger.info(f"Active campaigns found: {len(active_campaigns)}")

    params = {
        'access_token': ACCESS_TOKEN,
        'fields': 'campaign_name,ad_name,spend,impressions,clicks,actions,action_values',
        'time_range': f'{{"since":"{start_date}","until":"{end_date}"}}',
        'level': 'ad',
        'limit': 100
    }
    data = []
    retries = 3
    for attempt in range(retries):
        try:
            while True:
                response = requests.get(BASE_URL, params=params)
                response.raise_for_status()
                json_response = response.json()
                if 'data' not in json_response:
                    logger.error(f"Unexpected API response: {json_response}")
                    raise Exception("Unexpected API response: Missing 'data' key")
                
                # Filter data to only include ads from active campaigns
                filtered_data = []
                for ad_data in json_response.get('data', []):
                    # Extract campaign ID from ad data (you may need to adjust this based on actual API response)
                    campaign_name = ad_data.get('campaign_name', '')
                    # For now, we'll include all ads but log the filtering
                    # In a real implementation, you'd need to map campaign_name to campaign_id
                    filtered_data.append(ad_data)
                
                data.extend(filtered_data)
                logger.debug(f"Fetched {len(filtered_data)} records (filtered from {len(json_response.get('data', []))} total)")
                
                if 'paging' in json_response and 'next' in json_response['paging']:
                    params['after'] = json_response['paging']['cursors']['after']
                else:
                    break
            if not data:
                logger.warning("No data returned from Meta API")
            return data
        except requests.exceptions.HTTPError as e:
            if response.status_code == 429:
                logger.warning(f"Rate limit hit, retrying in {2 ** attempt} seconds...")
                time.sleep(2 ** attempt)
                continue
            elif response.status_code == 400:
                logger.error(f"Bad request error: {str(e)}")
                logger.warning("Meta API returned 400 error, returning empty data")
                return []
            elif response.status_code == 403:
                logger.error(f"403 Forbidden error from Meta API: {str(e)}")
                logger.warning("Returning empty data due to 403 error; will proceed with UTM/rollup data")
                return []
            logger.error(f"API request failed: {str(e)}")
            raise Exception(f"API request failed: {str(e)}")
        except requests.exceptions.RequestException as e:
            logger.error(f"API request failed: {str(e)}")
            raise Exception(f"API request failed: {str(e)}")

def calculate_net_roas(df, ad_col='ad_name'):
    logger.info("Starting net ROAS and profit calculation")
    logger.info(f"Input DataFrame columns: {df.columns.tolist()}")
    
    # Debug: Log sample of input data
    if not df.empty:
        logger.info("Sample input data (first 2 rows):")
        logger.info(df.head(2).to_string())

    # Ensure required columns exist and have proper numeric types
    numeric_columns = {
        'net_margin': 0,
        'purchases': 0,
        'spend': 0,
        'per_bottle_cost': 0
    }
    
    # Handle revenue column (either purchase_value or sales)
    revenue_column = 'purchase_value' if 'purchase_value' in df.columns else 'sales'
    if revenue_column not in df.columns:
        df[revenue_column] = 0  # Default to 0 if neither column exists
    numeric_columns[revenue_column] = 0
    
    # Convert all columns to numeric, filling missing values with 0
    for col, default in numeric_columns.items():
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(default)
            logger.debug(f"Converted {col} to numeric. Sample values: {df[col].head(2).to_list()}")
        else:
            df[col] = default
            logger.warning(f"Column {col} not found, using default value: {default}")
    
    if revenue_column in df.columns:
        df[revenue_column] = apply_net_revenue_column(df[revenue_column])
    
    # Debug: Log column statistics
    logger.info("\nColumn statistics after conversion:")
    for col in [revenue_column, 'purchases', 'per_bottle_cost', 'net_margin', 'spend']:
        if col in df.columns:
            logger.info(f"{col}: min={df[col].min():.2f}, max={df[col].max():.2f}, mean={df[col].mean():.2f}")

    # Calculate components for debugging
    df['cogs'] = df['purchases'] * df['per_bottle_cost']
    df['gross_profit'] = df[revenue_column] - df['cogs']
    df['net_profit'] = df['gross_profit'] - df['spend']
    
    # Calculate total margin and total spend
    df['total_margin'] = df['net_margin'] * df['purchases']
    total_margin = df['total_margin'].sum()
    total_spend = df['spend'].sum()
    
    # Calculate net ROAS as total margin divided by total ad spend
    df['net_roas'] = np.where(
        total_spend > 0,
        total_margin / total_spend,
        0
    ).round(4)
    
    # Debug: Log total values used in ROAS calculation
    logger.info("\nROAS Calculation Summary:")
    logger.info(f"  Total Margin: {total_margin:.2f}")
    logger.info(f"  Total Ad Spend: {total_spend:.2f}")
    if 'net_roas' in df.columns and not df['net_roas'].empty:
        logger.info(f"  Net ROAS: {df['net_roas'].iloc[0]:.4f} (Total Margin / Total Ad Spend)")
    else:
        logger.warning("  Net ROAS: Not available (either 'net_roas' column is missing or no data to report)")
    logger.info(f"  Total Purchases: {df['purchases'].sum():.0f}")
    logger.info(f"  Average Net Margin: {df['net_margin'].mean():.4f}")

    logger.info("Net ROAS calculated for ads:")
    for _, row in df[[ad_col, 'matched_sku', 'net_margin', 'purchases', 'spend', 'net_roas']].iterrows():
        logger.info(
            f"Ad: {row[ad_col]}, SKU: {row['matched_sku']}, Margin: {row['net_margin']}, "
            f"Purchases: {row['purchases']}, Spend: {row['spend']}, Net ROAS: {row['net_roas']}"
        )
    return df

def calculate_breakeven_roas(df, product_metrics_df, ad_col='ad_name'):
    df['per_bottle_cost'] = pd.to_numeric(df['per_bottle_cost'], errors='coerce').fillna(0)
    df['breakeven_roas'] = np.where(
        df['spend'] > 0,
        (df['per_bottle_cost'] * df['purchases'] + df['spend']) / df['spend'],
        0
    ).round(2)
    return df
    
def generate_ad_level_report(df, today, timestamp_str):
    from datetime import datetime
    if isinstance(today, str):
        today = datetime.strptime(today, "%Y-%m-%d")
    logger.info("Generating ad-level report")
    report_df = df[['ad_name', 'matched_sku', 'net_margin', 'purchases', 'spend', 'net_roas', 'net_profit', 'breakeven_roas']].copy()
    report_df.columns = ['Ad Name', 'Matched SKU', 'Net Margin', 'Purchases', 'Spend', 'Net ROAS', 'Net Profit', 'Breakeven ROAS']
    report_df = report_df.sort_values('Net ROAS', ascending=False)

    # Calculate sum of net profit
    total_net_profit = report_df['Net Profit'].sum()
    logger.info("Ad-level total net profit: %.2f", total_net_profit)

    # Create a safe filename using date and time without invalid characters
    safe_date = today.strftime("%Y-%m-%d")  # Format datetime object to YYYY-MM-DD
    safe_timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    _temp_dir = get_temp_dir()
    out_path = Path(os.path.join(_temp_dir, f"AdLevelReport-{safe_date}-{safe_timestamp}.xlsx"))
    with pd.ExcelWriter(out_path, engine='xlsxwriter') as writer:
        report_df.to_excel(writer, sheet_name='Ad Level Report', index=False)
    logger.info(f"Ad-level report generated: {out_path}")
    return out_path, total_net_profit

def process_data(raw_data, today, timestamp_str):
    """
    Process data for report generation. Since we're now using Shopify UTM data for sales,
    we only use API data for impressions and clicks, not for sales calculations.
    """
    if not raw_data:
        logger.warning("No data returned from API, but will try to generate report using combined UTM data")
        # Create empty DataFrame for API data with required columns
        df = pd.DataFrame(columns=['spend', 'impressions', 'clicks', 'actions', 'action_values', 'ad_name'])
    else:
        df = pd.DataFrame(raw_data)
        # Ensure required columns exist with default values for 0 sales scenarios
        required_columns = ['spend', 'impressions', 'clicks', 'actions', 'action_values', 'ad_name']
        for col in required_columns:
            if col not in df.columns:
                if col in ['spend', 'impressions', 'clicks']:
                    df[col] = 0
                elif col in ['actions', 'action_values']:
                    df[col] = ''  # Use empty string instead of empty list
                else:
                    df[col] = ''
        
        df['spend'] = pd.to_numeric(df['spend'], errors='coerce').fillna(0).astype(float)
        df['impressions'] = pd.to_numeric(df['impressions'], errors='coerce').fillna(0).astype(int)
        df['clicks'] = pd.to_numeric(df['clicks'], errors='coerce').fillna(0).astype(int)
        
        # Note: We're not extracting sales from API data anymore since we use Shopify UTM data
        # Set purchases and sales to 0 to avoid mixing API sales data with UTM sales data
        df['purchases'] = 0.0
        df['sales'] = 0.0
        if 'campaign_name' not in df.columns and 'ad_name' in df.columns:
            df['campaign_name'] = df['ad_name']
        for col, default in (
            ('net_margin', 0.0),
            ('per_bottle_cost', 0.0),
            ('breakeven_roas', 0.0),
            ('cogs', 0.0),
        ):
            if col not in df.columns:
                df[col] = default
        
        # Ad-level report is intentionally skipped because it is not sent in email attachments.
        ad_level_report_path = None
    
    # Get campaign metrics from combined UTM data instead of API data
    # Note: We're not passing API data to avoid mixing API and UTM data
    logger.info("Loading campaign metrics from dailyrollup...")
    campaign_summary = get_campaign_metrics_from_dailyrollup()
    
    logger.info("Campaign summary rows: %s", len(campaign_summary))
    
    if not campaign_summary.empty:
        # Segregate PMF campaigns
        pmf_campaigns = campaign_summary[campaign_summary['campaign_name'].str.contains('PMF', case=False, na=False)].copy()
        non_pmf_campaigns = campaign_summary[~campaign_summary['campaign_name'].str.contains('PMF', case=False, na=False)].copy()
        
        # Sort PMF campaigns by net ROAS (descending)
        if not pmf_campaigns.empty:
            pmf_campaigns = pmf_campaigns.sort_values('net_roas', ascending=False)
            logger.info(f"PMF campaigns found: {list(pmf_campaigns['campaign_name'].values)}")
        else:
            logger.info("No PMF campaigns found")
        
        # Sort and filter campaigns
        sorted_campaigns = campaign_summary.sort_values('net_roas', ascending=False)
        high_roas_campaigns = sorted_campaigns[sorted_campaigns['net_roas'] > 1]
        active_campaigns = sorted_campaigns
    else:
        # Fallback to API data if no combined UTM data available
        logger.warning("No combined UTM data available, falling back to API data for campaign metrics")
        if not df.empty:
            campaign_summary, high_roas_campaigns, active_campaigns, pmf_campaigns, non_pmf_campaigns = calculate_campaign_metrics(df)
        else:
            # No data available at all
            return {
                'total_sales': 0.0,
                'total_ad_spend': 0.0,
                'total_cogs': 0.0,
                'overall_roas': 0.0,
                'overall_net_roas': 0.0,
                'overall_cpp': 0.0,
                'overall_ctr': 0.0,
                'overall_conversion_rate': 0.0,
                'total_impressions': 0,
                'total_clicks': 0,
                'total_conversions': 0,
                'campaign_summary': pd.DataFrame(),
                'high_roas_campaigns': pd.DataFrame(),
                'active_campaigns': pd.DataFrame(),
                'pmf_campaigns': pd.DataFrame(),
                'non_pmf_campaigns': pd.DataFrame(),
                'ad_level_report': None
            }
    
    # Calculate overall metrics using combined UTM data if available
    if not campaign_summary.empty:
        # Use combined UTM data for overall metrics
        total_sales = campaign_summary['sales'].sum()
        total_ad_spend = campaign_summary['spend'].sum()
        total_cogs = campaign_summary['cogs'].sum() if 'cogs' in campaign_summary.columns else 0
        total_net_profit = campaign_summary['net_profit'].sum() if 'net_profit' in campaign_summary.columns else (total_sales - total_cogs - total_ad_spend)
        total_clicks = campaign_summary['clicks'].sum() if 'clicks' in campaign_summary.columns else 0
        total_purchases = campaign_summary['purchases'].sum() if 'purchases' in campaign_summary.columns else 0
        
        # Calculate overall metrics from campaign data
        overall_roas = 0 if total_ad_spend == 0 else np.nan_to_num(total_sales / total_ad_spend, nan=0, posinf=0, neginf=0).round(2)
        overall_net_roas = 0 if total_ad_spend == 0 else np.nan_to_num((total_sales - total_cogs) / total_ad_spend, nan=0, posinf=0, neginf=0).round(2)
        overall_cpp = 0 if total_purchases == 0 else np.nan_to_num(total_ad_spend / total_purchases, nan=0, posinf=0, neginf=0).round(2)
        overall_conversion_rate = 0 if total_clicks == 0 else np.nan_to_num((total_purchases / total_clicks) * 100, nan=0, posinf=0, neginf=0).round(2)
        overall_breakeven_roas = campaign_summary['breakeven_roas'].mean() if 'breakeven_roas' in campaign_summary.columns else 0
        
        # For impressions and CTR, still use API data as they're not in combined UTM
        total_impressions = df['impressions'].sum() if not df.empty else 0
        overall_ctr = 0 if total_impressions == 0 else np.nan_to_num((total_clicks / total_impressions) * 100, nan=0, posinf=0, neginf=0).round(2)
        total_margin = total_net_profit  # Use net profit as margin for combined UTM data
        
        logger.info(f"Using combined UTM data for metrics - Total Sales: {total_sales}, Total Spend: {total_ad_spend}, Total Net Profit: {total_net_profit}")
    else:
        # Fallback to API data for overall metrics
        if not df.empty:
            total_sales = df['sales'].sum()
            total_ad_spend = df['spend'].sum()
            total_cogs = df['cogs'].sum() if 'cogs' in df.columns else 0
            total_margin = df['total_margin'].sum() if 'total_margin' in df.columns else 0
            total_impressions = df['impressions'].sum()
            total_clicks = df['clicks'].sum()
            total_purchases = df['purchases'].sum()
            
            # Handle division by zero and infinity values safely
            overall_roas = 0 if total_ad_spend == 0 else np.nan_to_num(total_sales / total_ad_spend, nan=0, posinf=0, neginf=0).round(2)
            overall_net_roas = 0 if total_ad_spend == 0 else np.nan_to_num((total_sales - total_cogs) / total_ad_spend, nan=0, posinf=0, neginf=0).round(2)
            overall_cpp = 0 if total_purchases == 0 else np.nan_to_num(total_ad_spend / total_purchases, nan=0, posinf=0, neginf=0).round(2)
            overall_ctr = 0 if total_impressions == 0 else np.nan_to_num((total_clicks / total_impressions) * 100, nan=0, posinf=0, neginf=0).round(2)
            overall_conversion_rate = 0 if total_clicks == 0 else np.nan_to_num((total_purchases / total_clicks) * 100, nan=0, posinf=0, neginf=0).round(2)
            overall_breakeven_roas = df['breakeven_roas'].mean() if 'breakeven_roas' in df.columns else 0
            total_net_profit = total_sales - total_cogs - total_ad_spend
        else:
            # No data available
            total_sales = 0.0
            total_ad_spend = 0.0
            total_cogs = 0.0
            total_margin = 0.0
            total_impressions = 0
            total_clicks = 0
            total_purchases = 0
            overall_roas = 0.0
            overall_net_roas = 0.0
            overall_cpp = 0.0
            overall_ctr = 0.0
            overall_conversion_rate = 0.0
            overall_breakeven_roas = 0.0
            total_net_profit = 0.0
    
    # Calculate total net profit (if not already calculated)
    if 'total_net_profit' not in locals():
        total_net_profit = total_sales - total_cogs - total_ad_spend
    
    # Ad-level report is not generated in the email flow.
    ad_level_report_path = None
    
    # Fetch Google ad spend from googleads.py
    google_ad_spend = get_google_ads_total_spend()
    
    return {
        'total_sales': total_sales,
        'total_ad_spend': total_ad_spend,
        'google_ad_spend': google_ad_spend,
        'total_cogs': total_cogs,
        'overall_roas': overall_roas,
        'overall_net_roas': overall_net_roas,
        'overall_cpp': overall_cpp,
        'overall_ctr': overall_ctr,
        'overall_conversion_rate': overall_conversion_rate,
        'total_impressions': total_impressions,
        'total_clicks': total_clicks,
        'total_conversions': total_purchases,
        'campaign_summary': campaign_summary,
        'high_roas_campaigns': high_roas_campaigns,
        'active_campaigns': active_campaigns,
        'pmf_campaigns': pmf_campaigns,
        'non_pmf_campaigns': non_pmf_campaigns,
        'ad_level_report': ad_level_report_path,
        'total_net_profit': total_net_profit,
        'total_margin': total_margin,
        'overall_breakeven_roas': overall_breakeven_roas
    }

def generate_pdf_report(metrics, today, timestamp_str, timeframe_start=None, timeframe_end=None, report_type='daily'):
    """
    Generate a PDF report with the metrics using Shopify UTM data instead of API data
    
    Args:
        metrics: Dictionary containing campaign metrics and summaries
        today: Date object or string representing the report date
        timestamp_str: Timestamp string for file naming
        timeframe_start: Optional start date for the timeframe
        timeframe_end: Optional end date for the timeframe
        report_type: Type of report - 'daily', 'wtd' (Week-to-Date), or 'mtd' (Month-to-Date)
    
    Returns:
        str: Path to the generated PDF file
    """
    from datetime import datetime
    if isinstance(today, str):
        today = datetime.strptime(today, "%Y-%m-%d")
    # Create a safe filename using date and time without invalid characters
    safe_date = today.strftime("%Y-%m-%d")
    safe_timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    report_name = os.path.join(REPORT_DIR, f"DailySummary-PDF-{safe_date}-{safe_timestamp}.pdf")

    # Log timeframe for debugging
    try:
        if timeframe_start is not None and timeframe_end is not None:
            logger.info(
                "PDF summary metrics timeframe: start=%s, end=%s",
                (timeframe_start.astimezone(IST) if getattr(timeframe_start, 'tzinfo', None) else IST.localize(timeframe_start)).strftime('%Y-%m-%d %H:%M:%S %Z'),
                (timeframe_end.astimezone(IST) if getattr(timeframe_end, 'tzinfo', None) else IST.localize(timeframe_end)).strftime('%Y-%m-%d %H:%M:%S %Z'),
            )
        else:
            logger.info("PDF summary metrics timeframe: single-day (no explicit range provided)")
    except Exception:
        pass

    # Get organized metrics for the requested timeframe.
    # Primary source: General Statistics dashboard (dashboard_stats — API-first with a
    # ClickHouse gold fallback). Falls back to the legacy hourly endpoints
    # (get_organized_metrics_for_pdf) on any failure or when disabled.
    api_metrics = None
    if os.getenv("USE_DASHBOARD_STATS", "true").lower() in ("1", "true", "yes"):
        try:
            from dashboard_stats import get_dashboard_pdf_metrics
            api_metrics = get_dashboard_pdf_metrics(timeframe_start, timeframe_end)
            logger.info("PDF top-section metrics sourced from dashboard_stats (General Statistics)")
        except Exception as _de:
            logger.warning(
                "dashboard_stats failed (%s); falling back to hourly get_organized_metrics_for_pdf",
                _de,
            )
            api_metrics = None
    if api_metrics is None:
        api_metrics = get_organized_metrics_for_pdf(timeframe_start, timeframe_end)

    # Channel KPI rows: GET /v1/historical/dashboard (same as Selenic dashboard).
    # Entity Excel sheets still use fetch_marketing_hourly for campaign/SKU drill-down.
    # Set CHANNEL_FROM_ATTRIBUTION=true only for legacy reconciliation with attribution rollups.
    if os.getenv("CHANNEL_FROM_ATTRIBUTION", "false").lower() in ("1", "true", "yes") and isinstance(api_metrics, dict):
        try:
            from timeframe_config import get_timeframe_config
            from api_data_fetcher import fetch_marketing_hourly
            from dailyrollup import build_channel_summary_from_marketing_df
            _tf = get_timeframe_config(timeframe_start, timeframe_end)
            _cs = _tf['start_date'].strftime('%Y-%m-%d')
            _ce = _tf['end_date'].strftime('%Y-%m-%d')
            _attr_ch = build_channel_summary_from_marketing_df(fetch_marketing_hourly(_cs, _ce))
            for _ch in ("meta", "google", "organic"):
                if _ch in _attr_ch:
                    api_metrics[_ch] = _attr_ch[_ch]
            logger.info("Channel Performance rows overridden from attribution snapshot (CHANNEL_FROM_ATTRIBUTION=true)")
        except Exception as _cae:
            logger.warning("Channel-from-attribution reconcile skipped: %s", _cae)

    # Temporarily disabled — assigned but never used; PDF context reads api_metrics directly.
    # meta_metrics = api_metrics['meta']
    # google_metrics = api_metrics['google']
    # organic_metrics = api_metrics['organic']
    # total_metrics = api_metrics['total']

    global _daily_email_context
    try:
        # Derive date range string
        if timeframe_start is not None and timeframe_end is not None:
            _s = timeframe_start.astimezone(IST) if getattr(timeframe_start, 'tzinfo', None) else IST.localize(timeframe_start)
            _e = timeframe_end.astimezone(IST) if getattr(timeframe_end, 'tzinfo', None) else IST.localize(timeframe_end)
            date_range = f"{_s.strftime('%Y-%m-%d')} to {_e.strftime('%Y-%m-%d')}"
            start_str = _s.strftime('%Y-%m-%d')
            end_str = _e.strftime('%Y-%m-%d')
        else:
            date_range = today.strftime('%Y-%m-%d') if hasattr(today, 'strftime') else str(today)
            start_str = end_str = None
        report_time = datetime.now(IST).strftime('%H:%M')

        # Fetch supplementary data (errors are non-fatal).
        # Funnel + campaign performance are sourced from ClickHouse gold when
        # USE_CLICKHOUSE_FUNNEL is enabled (default), with automatic fallback to the
        # Postgres builders on any error or empty result.
        _use_ch_funnel = os.getenv("USE_CLICKHOUSE_FUNNEL", "true").lower() in ("1", "true", "yes")
        try:
            import clickhouse_report as _chr
        except Exception as _ie:
            logger.warning(f"clickhouse_report import failed; using Postgres funnel/campaigns: {_ie}")
            _use_ch_funnel = False

        try:
            funnel_metrics = None
            if _use_ch_funnel:
                try:
                    funnel_metrics = _chr.get_meta_funnel_metrics_ch(start_str, end_str)
                except Exception as _che:
                    logger.warning(f"ClickHouse Meta funnel failed; falling back to Postgres: {_che}")
                    funnel_metrics = None
            if not funnel_metrics:
                funnel_metrics = get_meta_funnel_metrics(start_date=start_str, end_date=end_str) if start_str else get_meta_funnel_metrics()
        except Exception as _fe:
            logger.warning(f"Meta funnel fetch failed: {_fe}")
            funnel_metrics = None

        try:
            google_funnel = None
            if _use_ch_funnel:
                try:
                    google_funnel = _chr.get_google_funnel_metrics_ch(timeframe_start, timeframe_end)
                except Exception as _che:
                    logger.warning(f"ClickHouse Google funnel failed; falling back to Postgres: {_che}")
                    google_funnel = None
            if not google_funnel:
                google_funnel = get_google_funnel_metrics(start_date=timeframe_start, end_date=timeframe_end)
        except Exception as _ge:
            logger.warning(f"Google funnel fetch failed: {_ge}")
            google_funnel = None

        try:
            campaign_df = None
            if _use_ch_funnel:
                try:
                    campaign_df = _chr.get_campaign_data_ch(start_str, end_str)
                except Exception as _che:
                    logger.warning(f"ClickHouse campaign data failed; falling back to Postgres: {_che}")
                    campaign_df = None
            if campaign_df is None or campaign_df.empty:
                campaign_df = get_campaign_data(start_date=start_str, end_date=end_str) if start_str else get_campaign_data()
        except Exception as _ce:
            logger.warning(f"Campaign data fetch failed: {_ce}")
            campaign_df = None

        # Temporarily disabled — PDF template section is commented out; only produced log warnings.
        # from channel_performance import reconcile_roas_with_pdf_metrics
        # reconcile_start = start_str or (
        #     today.strftime("%Y-%m-%d") if hasattr(today, "strftime") else str(today)[:10]
        # )
        # reconcile_end = end_str or reconcile_start
        # roas_reconciliation = reconcile_roas_with_pdf_metrics(
        #     api_metrics, reconcile_start, reconcile_end
        # )
        # if roas_reconciliation and not roas_reconciliation.get("all_match"):
        #     logger.warning("ROAS reconciliation mismatch for %s..%s", reconcile_start, reconcile_end)
        roas_reconciliation = None

        # Render PDF via weasyprint + Jinja2
        from report_renderer import build_daily_pdf_context, render_pdf_html, html_to_pdf
        pdf_ctx = build_daily_pdf_context(
            api_metrics=api_metrics,
            campaign_df=campaign_df,
            funnel_metrics=funnel_metrics,
            google_funnel=google_funnel,
            report_date=date_range,
            report_time=report_time,
            roas_reconciliation=roas_reconciliation,
        )
        if report_type == 'wtd':
            pdf_ctx['report_title'] = 'Week-to-Date Marketing Performance Report'
        elif report_type == 'mtd':
            pdf_ctx['report_title'] = 'Month-to-Date Marketing Performance Report'
        else:
            pdf_ctx['report_title'] = 'Daily Marketing Performance Report'

        html = render_pdf_html(pdf_ctx)
        html_to_pdf(html, report_name)

        # Cache context for send_email; reuse pdf_ctx's normalized funnel
        from report_renderer import build_returns_row
        _email_total = api_metrics.get("total", {})
        _email_amazon = api_metrics.get("amazon", {})
        _email_channels = [
            api_metrics.get("meta", {}),
            api_metrics.get("google", {}),
            api_metrics.get("organic", {}),
        ]
        # Amazon is a separate marketplace; show it as a channel row only when the
        # source provides it, so the channel rows + returns row reconcile to Total.
        if _email_amazon:
            _email_channels.append(_email_amazon)
        _daily_email_context = {
            "meta": api_metrics.get("meta", {}),
            "google": api_metrics.get("google", {}),
            "organic": api_metrics.get("organic", {}),
            "amazon": _email_amazon,
            "total": _email_total,
            "returns_row": build_returns_row(
                _email_total,
                _email_channels,
                returns_cancels_count=int(_email_total.get("returns_cancels") or 0),
            ),
            "funnel": pdf_ctx.get("funnel"),
            "google_funnel": google_funnel,
            "campaigns": pdf_ctx.get("campaigns", []),
        }

        logger.info(f"PDF report saved to: {report_name}")
        return report_name
    except Exception as e:
        logger.error(f"Failed to generate PDF report: {str(e)}")
        raise


def _simple_email_body(today_str: str, chart_cids: list) -> str:
    """Minimal fallback email body used when the Jinja2 template can't render."""
    imgs = "".join(
        f'<p><img src="cid:{cid}" alt="chart" style="max-width:600px;width:100%;height:auto;"></p>'
        for cid in chart_cids
    )
    return (
        f'<html><body style="font-family:Arial,sans-serif;color:#333;">'
        f'<p>Daily Marketing Performance Report — <strong>{today_str}</strong></p>'
        f'{imgs}'
        f'<p><em>Full data in attached Excel &amp; PDF.</em></p>'
        f'</body></html>'
    )


def _cleanup_paths(paths: list) -> None:
    """Remove temp files and empty parent dirs after a successful email send."""
    seen_dirs: set[str] = set()
    for file_path in paths:
        if not file_path or not os.path.exists(file_path):
            continue
        try:
            parent = os.path.dirname(file_path)
            if parent:
                seen_dirs.add(parent)
            os.remove(file_path)
            logger.info("Deleted file: %s", file_path)
        except Exception as e:
            logger.warning("Failed to delete file %s: %s", file_path, e)
    for parent in seen_dirs:
        try:
            if os.path.isdir(parent) and not os.listdir(parent):
                os.rmdir(parent)
                logger.info("Removed empty directory: %s", parent)
        except Exception as e:
            logger.warning("Failed to remove directory %s: %s", parent, e)


def send_email(excel_file_param, pdf_file_param, ad_level_report_param, today, timestamp_str, plot_files_param=None, product_report_param=None, file_paths_to_attach=None):
    logger.info("Preparing to send email via Microsoft Graph API...")
    # Use the global config values loaded from database at the top of the file
    client_id = CLIENT_ID  # From get_global_config('AZURE_CLIENT_ID')
    client_secret = CLIENT_SECRET  # From get_global_config('AZURE_CLIENT_SECRET')
    tenant_id = TENANT_ID  # From get_global_config('AZURE_TENANT_ID')
    email_sender = os.getenv('EMAIL_SENDER')
    email_recipients = [e.strip() for e in os.getenv('EMAIL_RECIPIENTS', '').split(',') if e.strip()]
    if not all([client_id, client_secret, tenant_id, email_sender, email_recipients]):
        logger.error("Missing required environment variables for email configuration.")
        raise Exception("Missing required environment variables for email configuration.")
    authority = f'https://login.microsoftonline.com/{tenant_id}'
    scope = ["https://graph.microsoft.com/.default"]
    app = msal.ConfidentialClientApplication(client_id, authority=authority, client_credential=client_secret)
    result = app.acquire_token_for_client(scopes=scope)
    print(result)
    if "access_token" not in result:
        logger.error("Failed to acquire access token for Microsoft Graph API.")
        raise Exception("Failed to acquire access token for Microsoft Graph API.")
    access_token = result["access_token"]
    # Handle the case where today is already a datetime object
    if isinstance(today, datetime):
        weekday = today.strftime('%A')
    else:
        weekday = datetime.strptime(today, '%Y-%m-%d').strftime('%A')
    # Format today for the subject line
    today_str = today.strftime('%Y-%m-%d') if isinstance(today, datetime) else today

    subject = f"Daily Marketing Performance Report - {today_str}"

    daily_plot_cid = None
    historical_plot_cid = None
    shopify_profit_plot_cid = None
    hourly_sales_plot_cid = None  # NEW: For hourly sales plot
    sales_by_state_pie_cid = None
    channel_performance_cid = None
    weather_plot_cid = None

    graph_api_attachments = [] # This will hold the attachments in Graph API format

    if file_paths_to_attach:
        logger.info(f"Processing {len(file_paths_to_attach)} attachments for email")
        for file_path in file_paths_to_attach:
            if not file_path or not os.path.exists(file_path):
                logger.warning(f"File not found or invalid path, skipping attachment: {file_path}")
                continue

            try:
                with open(file_path, "rb") as f:
                    file_content = base64.b64encode(f.read()).decode('utf-8')
            except Exception as e:
                logger.error(f"Error reading file {file_path} for attachment: {str(e)}")
                continue
            
            base_filename = os.path.basename(file_path)
            descriptive_name = base_filename
            content_id = None
            is_inline = False

            # Determine descriptive name, content ID, and inline status based on file type
            if 'daily_insights' in base_filename:
                descriptive_name = f"Daily Performance Plot - {today_str}.png"
                daily_plot_cid = f"daily_plot_{today_str}"
                content_id = daily_plot_cid
                is_inline = True
            elif 'daily_shopify_profit' in base_filename:
                descriptive_name = f"Daily Shopify Net Profit - {today_str}.png"
                shopify_profit_plot_cid = f"shopify_profit_plot_{today_str}"
                content_id = shopify_profit_plot_cid
                is_inline = True
            elif 'hourly_sales_last_7_days' in base_filename:  # NEW: Hourly sales plot
                descriptive_name = f"Hourly Sales (Last 7 Days) - {today_str}.png"
                hourly_sales_plot_cid = f"hourly_sales_plot_{today_str}"
                content_id = hourly_sales_plot_cid
                is_inline = True
            elif 'sales_by_state_pie' in base_filename:
                descriptive_name = f"Sales by state — pie chart - {today_str}.png"
                sales_by_state_pie_cid = f"sales_by_state_pie_{today_str}"
                content_id = sales_by_state_pie_cid
                is_inline = True
            elif 'channel_performance_daily' in base_filename:
                descriptive_name = f"Channel Performance (Daily) - {today_str}.png"
                m = re.search(r"channel_performance_daily_(\d{4}-\d{2}-\d{2})", base_filename)
                chart_day = m.group(1) if m else today_str
                channel_performance_cid = f"channel_performance_daily_{chart_day}"
                content_id = channel_performance_cid
                is_inline = True
            elif 'historical_insights' in base_filename:
                descriptive_name = f"Historical Trends Plot - {today_str}.png"
                historical_plot_cid = f"historical_plot_{today_str}"
                content_id = historical_plot_cid
                is_inline = True
            elif 'campaign_opportunity_combined' in base_filename:
                descriptive_name = f"Weather Campaign Opportunity - {today_str}.png"
                weather_plot_cid = f"weather_campaign_{today_str}"
                content_id = weather_plot_cid
                is_inline = True
            elif base_filename.startswith('campaign_opportunity_') and base_filename.endswith('.csv'):
                descriptive_name = f"Weather Campaign Opportunity - {today_str}.csv"
            elif 'AdLevelReport' in base_filename:
                descriptive_name = f"Ad-Level Report - {today_str}.xlsx"
            elif 'DailySummary-PDF' in base_filename:
                descriptive_name = f"Marketing Summary Report - {today_str}.pdf"
            elif 'ProductDetailedReport' in base_filename:
                descriptive_name = f"Product Detailed Report - {today_str}.xlsx"
            elif 'Analysis_of_Seleric' in base_filename: # Assuming this is the intelligence PDF format
                descriptive_name = f"Intelligence Analysis - {today_str}.pdf"
            elif 'Hourly Drill Down' in base_filename: # Assuming this is the raw hourly data
                descriptive_name = f"Raw Hourly Data - {today_str}.xlsx"
            elif 'Entity report' in base_filename: # Entity report from dailyrollup
                descriptive_name = f"Entity Report - {today_str}.xlsx"
            
            attachment_dict = {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": descriptive_name,
                "contentBytes": file_content
            }
            if content_id and is_inline:
                attachment_dict["contentId"] = content_id
                attachment_dict["isInline"] = is_inline
            
            graph_api_attachments.append(attachment_dict)
            logger.info(f"Added attachment: {descriptive_name} (Inline: {is_inline})")

    # Collect chart CIDs for template rendering
    chart_cids = [
        c for c in [
            daily_plot_cid,
            shopify_profit_plot_cid,
            channel_performance_cid,
            sales_by_state_pie_cid,
            hourly_sales_plot_cid,
        ] if c
    ]

    # Render rich HTML email using Jinja2 template when PDF context is available
    if _daily_email_context is not None:
        try:
            from report_renderer import render_email_daily
            _daily_email_context.update({
                "chart_cids": chart_cids,
                "weather_chart_cid": weather_plot_cid,
                "report_date": today_str,
                "report_time": datetime.now(IST).strftime("%H:%M"),
                "weekday": weekday,
            })
            email_body = render_email_daily(_daily_email_context)
        except Exception as _render_err:
            logger.warning(f"Email template render failed: {_render_err}; using fallback body")
            email_body = _simple_email_body(today_str, chart_cids)
    else:
        email_body = _simple_email_body(today_str, chart_cids)

    body = {
        "contentType": "HTML",
        "content": email_body
    }
    
    message = {
        "message": {
            "subject": subject,
            "body": body,
            "toRecipients": [{"emailAddress": {"address": addr}} for addr in email_recipients],
            "attachments": graph_api_attachments,
        },
        "saveToSentItems": "true"
    }
    logger.info("Sending email via Microsoft Graph API...")
    graph_url = f"https://graph.microsoft.com/v1.0/users/{email_sender}/sendMail"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    response = requests.post(graph_url, headers=headers, json=message)
    if response.status_code == 202:
        logger.info("Email sent successfully")
        
        # Use the original list of paths for deletion
        _cleanup_paths([p for p in (file_paths_to_attach or []) if p])
        
        # Delete ad level report if it exists
        if ad_level_report_param and os.path.exists(ad_level_report_param):
            try:
                os.remove(ad_level_report_param)
                logger.info(f"Deleted ad level report: {ad_level_report_param}")
            except Exception as e:
                logger.warning(f"Failed to delete ad level report {ad_level_report_param}: {e}")
    else:
        logger.error(f"Failed to send email: {response.status_code} {response.text}")
        raise Exception(f"Failed to send email: {response.status_code} {response.text}")



def calculate_campaign_metrics(df):
    """Calculate campaign-level metrics and summaries.
    
    Args:
        df (DataFrame): DataFrame containing campaign data
    
    Returns:
        tuple: (campaign_summary, high_roas_campaigns, active_campaigns, pmf_campaigns, non_pmf_campaigns)
    """
    if df is None or df.empty:
        empty = pd.DataFrame()
        return empty, empty, empty, empty, empty

    work = df.copy()
    if 'campaign_name' not in work.columns:
        if 'ad_name' in work.columns:
            work['campaign_name'] = work['ad_name']
        else:
            work['campaign_name'] = 'Unknown'

    defaults = {
        'spend': 0.0,
        'sales': 0.0,
        'impressions': 0,
        'clicks': 0,
        'purchases': 0.0,
        'net_margin': 0.0,
        'per_bottle_cost': 0.0,
        'breakeven_roas': 0.0,
        'cogs': 0.0,
    }
    for col, default in defaults.items():
        if col not in work.columns:
            work[col] = default
        else:
            work[col] = pd.to_numeric(work[col], errors='coerce').fillna(default)

    if (work['breakeven_roas'] == 0).all() and work['spend'].sum() > 0:
        work['breakeven_roas'] = np.where(
            work['spend'] > 0,
            (work['per_bottle_cost'] * work['purchases'] + work['spend']) / work['spend'],
            0,
        ).round(2)

    # Calculate campaign summary
    campaign_summary = work.groupby('campaign_name').agg({
        'spend': 'sum',
        'sales': 'sum',
        'impressions': 'sum',
        'clicks': 'sum',
        'purchases': 'sum',
        'breakeven_roas': 'mean'
    }).reset_index()
    
    # Calculate margin and ROAS metrics
    work['total_margin'] = work['net_margin'] * work['purchases']
    campaign_summary['total_margin'] = work.groupby('campaign_name')['total_margin'].sum().values
    
    # Calculate key performance metrics
    metrics = {
        'roas': ('sales', 'spend'),
        'net_roas': ('total_margin', 'spend'),
        'cpp': ('spend', 'purchases'),
        'ctr': ('clicks', 'impressions', 100),  # Multiply by 100 for percentage
        'conversion_rate': ('purchases', 'clicks', 100)  # Multiply by 100 for percentage
    }
    
    for metric, cols in metrics.items():
        numerator, denominator = cols[:2]
        multiplier = cols[2] if len(cols) > 2 else 1
        campaign_summary[metric] = (
            (campaign_summary[numerator] / campaign_summary[denominator] * multiplier)
            .replace([float('inf'), -float('inf')], 0)
            .fillna(0)
            .round(2)
        )
    
    # Segregate PMF campaigns
    pmf_campaigns = campaign_summary[campaign_summary['campaign_name'].str.contains('PMF', case=False, na=False)].copy()
    non_pmf_campaigns = campaign_summary[~campaign_summary['campaign_name'].str.contains('PMF', case=False, na=False)].copy()
    
    # Sort PMF campaigns by net ROAS (descending)
    if not pmf_campaigns.empty:
        pmf_campaigns = pmf_campaigns.sort_values('net_roas', ascending=False)
        logger.info(f"PMF campaigns found: {list(pmf_campaigns['campaign_name'].values)}")
    else:
        logger.info("No PMF campaigns found")
    
    # PMF campaigns use normal calculations (no simplified metrics)
    # They will be displayed in a separate section in the PDF report
    
    # Sort and filter campaigns
    sorted_campaigns = campaign_summary.sort_values('net_roas', ascending=False)
    high_roas_campaigns = sorted_campaigns[sorted_campaigns['net_roas'] > 1]
    active_campaigns = sorted_campaigns
    
    return campaign_summary, high_roas_campaigns, active_campaigns, pmf_campaigns, non_pmf_campaigns

def generate_product_wise_sheet(df, product_metrics_df, shopify_data):
    """
    Generate a product-wise data sheet with metrics from Shopify purchases and ad data.
    
    Args:
        df: DataFrame containing ad data
        product_metrics_df: DataFrame containing product metrics
        shopify_data: List of dictionaries containing Shopify order data
        
    Returns:
        DataFrame: Product-wise metrics for Excel sheet
    """
    logger.info("Generating product-wise data sheet...")
    
    # Create a DataFrame from Shopify data if it's not empty
    if shopify_data and len(shopify_data) > 0:
        # Ensure all numeric fields are properly converted to float
        for item in shopify_data:
            for key in ['quantity', 'price', 'per_bottle_cost', 'net_margin_per_product', 'total_price']:
                if key in item and item[key] is not None:
                    try:
                        item[key] = float(item[key])
                    except (ValueError, TypeError):
                        logger.warning(f"Invalid numeric value for {key}: {item[key]}, setting to 0")
                        item[key] = 0.0
        
        shopify_df = pd.DataFrame(shopify_data)
        logger.info(f"Shopify data loaded with {len(shopify_df)} rows")
    else:
        logger.warning("No Shopify data available, creating empty DataFrame")
        shopify_df = pd.DataFrame(columns=['product_name', 'sku', 'quantity', 'price', 'per_bottle_cost', 'net_margin_per_product'])
    
    # Group Shopify data by product
    if not shopify_df.empty:
        shopify_product_summary = shopify_df.groupby('product_name').agg({
            'quantity': 'sum',
            'price': 'mean',
            'per_bottle_cost': 'mean',
            'net_margin_per_product': 'mean',
            'total_price': 'sum',
            'sku': lambda x: x.iloc[0] if x.notna().any() else 'Unknown'
        }).reset_index()
        
        # Rename columns for clarity
        shopify_product_summary.rename(columns={
            'quantity': 'Indirect_ad_purchase',
            'price': 'MRP',
            'per_bottle_cost': 'COGS',
            'net_margin_per_product': 'Margin',
            'total_price': 'Total_Sales'
        }, inplace=True)
    else:
        # Create empty DataFrame with required columns if no Shopify data
        shopify_product_summary = pd.DataFrame(columns=[
            'product_name', 'sku', 'Indirect_ad_purchase', 'MRP', 'COGS', 'Margin', 'Total_Sales'
        ])
    
    # Group ad data by matched_sku to get ad spend per product
    if not df.empty:
        # Ensure matched_sku column exists
        if 'matched_sku' not in df.columns:
            logger.warning("'matched_sku' column not found in ad data")
            df['matched_sku'] = 'Unknown'
            
        ad_spend_by_product = df.groupby('matched_sku').agg({
            'spend': 'sum',
            'purchase_value': 'sum'
        }).reset_index()
        
        # Rename for clarity
        ad_spend_by_product.rename(columns={
            'matched_sku': 'sku',
            'spend': 'Total_Ad_spent',
            'purchase_value': 'Ad_Sales'
        }, inplace=True)
    else:
        ad_spend_by_product = pd.DataFrame(columns=['sku', 'Total_Ad_spent', 'Ad_Sales'])
    
    # Merge Shopify data with ad spend data
    product_wise_df = pd.merge(shopify_product_summary, ad_spend_by_product, on='sku', how='outer')
    
    # Fill NaN values with 0 and ensure all numeric columns are float type
    numeric_columns = ['Indirect_ad_purchase', 'MRP', 'COGS', 'Margin', 'Total_Sales', 'Total_Ad_spent', 'Ad_Sales']
    
    # Handle each numeric column individually to catch and fix any conversion errors
    for col in numeric_columns:
        if col in product_wise_df.columns:
            try:
                # First convert to string to handle any concatenated values, then to float
                product_wise_df[col] = product_wise_df[col].fillna(0)
                product_wise_df[col] = product_wise_df[col].apply(
                    lambda x: float(str(x).split('.')[0] + '.' + str(x).split('.')[1][:2]) if isinstance(x, str) and '.' in x else float(x)
                )
            except Exception as e:
                logger.warning(f"Error converting column {col} to float: {str(e)}. Using zeros instead.")
                product_wise_df[col] = 0.0
    
    # Calculate ROAS metrics using np.nan_to_num to safely handle division
    try:
        product_wise_df['Gross_ROAS'] = product_wise_df.apply(
            lambda row: np.nan_to_num(row['Total_Sales'] / row['Total_Ad_spent'], nan=0, posinf=0, neginf=0) 
            if row['Total_Ad_spent'] > 0 else 0, axis=1
        ).round(2)
        
        product_wise_df['Net_ROAS'] = product_wise_df.apply(
            lambda row: np.nan_to_num((row['Margin'] * row['Indirect_ad_purchase']) / row['Total_Ad_spent'], nan=0, posinf=0, neginf=0) 
            if row['Total_Ad_spent'] > 0 else 0, axis=1
        ).round(2)
        
        # Calculate ROI: (Revenue - Cost) / Cost
        product_wise_df['ROI'] = product_wise_df.apply(
            lambda row: np.nan_to_num(((row['Total_Sales'] - row['Total_Ad_spent']) / row['Total_Ad_spent']) * 100, nan=0, posinf=0, neginf=0) 
            if row['Total_Ad_spent'] > 0 else 0, axis=1
        ).round(2)
    except Exception as e:
        logger.error(f"Error calculating ROAS/ROI metrics: {str(e)}")
        # Set default values if calculation fails
        product_wise_df['Gross_ROAS'] = 0
        product_wise_df['Net_ROAS'] = 0
        product_wise_df['ROI'] = 0
    
    # Rename product_name column to match requested output
    product_wise_df.rename(columns={'product_name': 'Product name'}, inplace=True)
    
    # Reorder columns as requested
    final_columns = [
        'Product name', 'Indirect_ad_purchase', 'MRP', 'COGS', 'Margin', 
        'Total_Sales', 'Total_Ad_spent', 'Gross_ROAS', 'Net_ROAS', 'ROI'
    ]
    
    # Ensure all required columns exist
    for col in final_columns:
        if col not in product_wise_df.columns:
            product_wise_df[col] = 0
    
    # Return DataFrame with columns in requested order
    return product_wise_df[final_columns]

def export_hourly_metrics_with_diff(today, timestamp_str):
    """
    Export hourly metrics with difference calculations to Excel
    """
    # Function commented out as requested
    logger.info("export_hourly_metrics_with_diff function is currently disabled")
    return None

def export_raw_hourly_data(today, timestamp_str):
    import time
    from sqlalchemy.exc import OperationalError
    
    # Add sslmode=require to ensure secure connection
    conn_str = (
        f"postgresql://{DB_CONFIG['user']}:{DB_CONFIG['password']}@"
        f"{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}?sslmode=require"
    )
    
    # Set up retry parameters
    max_retries = 3
    retry_delay = 2  # seconds
    
    for attempt in range(max_retries):
        try:
            logger.info(f"Connecting to database (attempt {attempt+1}/{max_retries})")
            engine = create_engine(conn_str, isolation_level="AUTOCOMMIT", connect_args={'connect_timeout': 10})

            # Test the connection first
            with engine.connect():
                logger.info("Database connection successful")

            query = f"SELECT * FROM ad_insights_hourly_snapshots WHERE DATE(snapshot_hour) = %s"
            df = pd.read_sql(query, engine, params=(today,))

            _temp_dir = get_temp_dir()
            out_path = Path(os.path.join(_temp_dir, f"Hourly Drill Down-{today}-{timestamp_str.split('/')[1].strip()[:-3].replace(' ', '-')}.xlsx"))
            with pd.ExcelWriter(out_path, engine='xlsxwriter') as writer:
                df.to_excel(writer, sheet_name='Raw Hourly Data', index=False)

            logger.info(f"Exported raw hourly snapshot data to {out_path}")
            return out_path

        except OperationalError as e:
            logger.error(f"Database connection error (attempt {attempt+1}/{max_retries}): {str(e)}")
            if attempt < max_retries - 1:
                logger.info(f"Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
                retry_delay *= 2
                continue
            else:
                logger.error("Maximum retry attempts reached. Could not connect to database.")
                return None
        except Exception as e:
            logger.error(f"Error exporting raw hourly data: {str(e)}", exc_info=True)
            return None

def get_campaign_metrics_from_dailyrollup():
    """
    Get campaign metrics from combinedUTMMeta data.
    This function fetches the combined UTM data and aggregates it by campaign.
    Only includes campaigns with actual sales from Shopify UTM data.
    
    Returns:
        DataFrame: Campaign-level metrics with sales, spend, and calculated ROAS metrics
    """
    try:
        logger.info("Fetching campaign metrics from dailyrollup campaign data...")
        dailyrollup_campaign_df = get_campaign_data()
        logger.info("Campaign rows fetched: %s", len(dailyrollup_campaign_df))
        
        if not dailyrollup_campaign_df.empty:
            # Start from a copy; add/alias columns needed for PDF tables
            df = dailyrollup_campaign_df.copy()
            
            # Ensure both 'sales' and 'shopify_revenue' columns exist for compatibility
            if 'sales' not in df.columns:
                if 'revenue' in df.columns:
                    df['sales'] = df['revenue']
                elif 'shopify_revenue' in df.columns:
                    df['sales'] = df['shopify_revenue']
            if 'shopify_revenue' not in df.columns:
                if 'sales' in df.columns:
                    df['shopify_revenue'] = df['sales']
                elif 'revenue' in df.columns:
                    df['shopify_revenue'] = df['revenue']
            # Alias be_roas if only breakeven_roas exists
            if 'be_roas' not in df.columns and 'breakeven_roas' in df.columns:
                df['be_roas'] = df['breakeven_roas']
            # Provide 'roas' alias from gross_roas for any downstream compatibility
            if 'roas' not in df.columns and 'gross_roas' in df.columns:
                df['roas'] = df['gross_roas']
            # Ensure expected columns exist with defaults
            needed = [
                'campaign_name','spend','shopify_revenue','sales','ctr','bounce_rate',
                'gross_roas','net_roas','be_roas','conversion_rate','cpp','net_profit'
            ]
            for col in needed:
                if col not in df.columns:
                    df[col] = 0
            df_out = df[needed + [c for c in ['purchases','clicks','roas','net_profit','cogs'] if c in df.columns]]
            
            logger.info("Campaign summary prepared: %s rows", len(df_out))
            
            return df_out

        logger.warning("No dailyrollup campaign data available")
        return pd.DataFrame()
        
    except Exception as e:
        logger.error(f"Error getting campaign metrics from combined UTM: {str(e)}")
        logger.error("Campaign metrics exception details: %s", str(e), exc_info=True)
        return pd.DataFrame()
        
def get_organized_metrics_from_utm(start_date=None, end_date=None):
    """
    Get organized metrics for PDF report from combined UTM data instead of API data.
    Returns the same structure as get_organized_metrics_for_pdf() but using Shopify UTM data.
    
    Note: The combined UTM data only includes Meta campaigns, so Google and Organic metrics
    are calculated as follows:
    - Meta: Direct from combined UTM data
    - Google: Set to 0 (not included in combined UTM data)
    - Organic: Calculated as Total - Meta - Google (where Google = 0)
    """
    try:
        # If a timeframe is provided, aggregate per-day metrics across the range
        if start_date is not None and end_date is not None:
            from datetime import timedelta
            import combinedUTMMeta as cum
            import conversionTracking as ct

            # Initialize accumulators
            meta_sales = 0
            meta_cogs = 0
            meta_ad_spend = 0
            meta_net_profit = 0
            meta_orders = 0

            google_sales = 0
            google_cogs = 0
            google_ad_spend = 0
            google_net_profit = 0
            google_orders = 0

            all_sales = 0
            all_cogs = 0
            all_orders = 0

            # Iterate inclusive over the date range (use date objects)
            current_day = start_date.astimezone(IST).date() if getattr(start_date, 'tzinfo', None) else IST.localize(start_date).date()
            last_day = end_date.astimezone(IST).date() if getattr(end_date, 'tzinfo', None) else IST.localize(end_date).date()
            try:
                logger.info("Summary metrics aggregation range (inclusive): %s to %s", current_day.strftime('%Y-%m-%d'), last_day.strftime('%Y-%m-%d'))
            except Exception:
                pass
            while current_day <= last_day:
                try:
                    logger.info("Processing summary metrics for day: %s", current_day.strftime('%Y-%m-%d'))
                except Exception:
                    pass
                # Monkey-patch helpers to force modules to use current_day
                orig_cum_today = getattr(cum, 'get_today_ist', None)
                orig_ct_today = getattr(ct, 'get_today_ist', None)
                try:
                    if orig_cum_today is not None:
                        cum.get_today_ist = lambda: current_day
                    if orig_ct_today is not None:
                        ct.get_today_ist = lambda: current_day

                    # Meta per-day (combined UTM Meta-only data)
                    daily_meta_df = cum.get_combined_utm_report_df()
                    if daily_meta_df is not None and not daily_meta_df.empty:
                        # Sum all rows including additional meta spend to match full daily spend
                        d_sales = float(daily_meta_df['total_sales'].sum())
                        d_cogs = float(daily_meta_df['total_cogs'].sum())
                        d_spend = float(daily_meta_df['ad_spent'].sum())
                        d_net = float(daily_meta_df['net_profit'].sum())
                        meta_sales += d_sales
                        meta_cogs += d_cogs
                        meta_ad_spend += d_spend
                        meta_net_profit += d_net
                        try:
                            logger.info("Meta daily totals [%s]: sales=%.2f cogs=%.2f spend=%.2f net=%.2f", current_day.strftime('%Y-%m-%d'), d_sales, d_cogs, d_spend, d_net)
                        except Exception:
                            pass
                        # Orders present as 'orders'
                        if 'orders' in daily_meta_df.columns:
                            d_orders = int(daily_meta_df['orders'].sum())
                            meta_orders += d_orders
                            try:
                                logger.info("Meta daily orders [%s]: %d", current_day.strftime('%Y-%m-%d'), d_orders)
                            except Exception:
                                pass

                    # Google per-day
                    daily_google = ct.get_google_metrics()
                    if isinstance(daily_google, dict) and daily_google:
                        g_sales = float(daily_google.get('total_sales', 0) or 0)
                        g_cogs = float(daily_google.get('total_cogs', 0) or 0)
                        g_spend = float(daily_google.get('google_ad_spent', 0) or 0)
                        g_net = float(daily_google.get('net_profit', 0) or 0)
                        g_orders = int(daily_google.get('total_orders', 0) or 0)
                        google_sales += g_sales
                        google_cogs += g_cogs
                        google_ad_spend += g_spend
                        google_net_profit += g_net
                        google_orders += g_orders
                        try:
                            logger.info("Google daily totals [%s]: sales=%.2f cogs=%.2f spend=%.2f net=%.2f orders=%d", current_day.strftime('%Y-%m-%d'), g_sales, g_cogs, g_spend, g_net, g_orders)
                        except Exception:
                            pass

                    # All sources (for organic remainder)
                    try:
                        full_df, total_row = ct.get_conversion_tracking_report_df()
                        if isinstance(total_row, pd.Series):
                            t_sales = float(total_row.get('total_sales', 0) or 0)
                            t_cogs = float(total_row.get('total_cogs', 0) or 0)
                            t_orders = int(total_row.get('orders', 0) or 0)
                            all_sales += t_sales
                            all_cogs += t_cogs
                            all_orders += t_orders
                            try:
                                logger.info("All sources daily totals [%s]: sales=%.2f cogs=%.2f orders=%d", current_day.strftime('%Y-%m-%d'), t_sales, t_cogs, t_orders)
                            except Exception:
                                pass
                        elif isinstance(full_df, pd.DataFrame) and not full_df.empty:
                            t_sales = float(full_df['total_sales'].sum())
                            t_cogs = float(full_df['total_cogs'].sum())
                            all_sales += t_sales
                            all_cogs += t_cogs
                            if 'orders' in full_df.columns:
                                t_orders = int(full_df['orders'].sum())
                                all_orders += t_orders
                            try:
                                logger.info("All sources daily totals [%s] (from df): sales=%.2f cogs=%.2f orders=%d", current_day.strftime('%Y-%m-%d'), t_sales, t_cogs, t_orders if 't_orders' in locals() else 0)
                            except Exception:
                                pass
                    except Exception:
                        pass
                finally:
                    # Restore original functions
                    if orig_cum_today is not None:
                        cum.get_today_ist = orig_cum_today
                    if orig_ct_today is not None:
                        ct.get_today_ist = orig_ct_today

                current_day += timedelta(days=1)

            # Derive organic by subtraction
            organic_sales = max(0.0, all_sales - meta_sales - google_sales)
            organic_cogs = max(0.0, all_cogs - meta_cogs - google_cogs)
            organic_net_profit = organic_sales - organic_cogs
            organic_orders = max(0, all_orders - meta_orders - google_orders)

            total_sales = meta_sales + google_sales + organic_sales
            total_cogs = meta_cogs + google_cogs + organic_cogs
            total_ad_spent = meta_ad_spend + google_ad_spend
            total_net_profit = meta_net_profit + google_net_profit + organic_net_profit
            total_orders = meta_orders + google_orders + organic_orders
            try:
                logger.info("Aggregated totals: meta_sales=%.2f google_sales=%.2f organic_sales=%.2f", meta_sales, google_sales, organic_sales)
                logger.info("Aggregated totals: meta_spend=%.2f google_spend=%.2f total_spend=%.2f", meta_ad_spend, google_ad_spend, total_ad_spent)
                logger.info("Aggregated totals: meta_orders=%d google_orders=%d organic_orders=%d total_orders=%d", meta_orders, google_orders, organic_orders, total_orders)
            except Exception:
                pass

            # Compute ROAS/CPP for each bucket
            def safe_div(n, d):
                return float(n) / float(d) if d and float(d) != 0 else 0.0

            meta_gross_roas = safe_div(meta_sales, meta_ad_spend)
            meta_net_roas = safe_div(meta_sales - meta_cogs, meta_ad_spend)
            meta_be_roas = safe_div(meta_cogs + meta_ad_spend, meta_ad_spend)
            meta_cpp = safe_div(meta_ad_spend, meta_orders)

            google_gross_roas = safe_div(google_sales, google_ad_spend)
            google_net_roas = safe_div(google_sales - google_cogs, google_ad_spend)
            google_be_roas = safe_div(google_cogs + google_ad_spend, google_ad_spend)
            google_cpp = safe_div(google_ad_spend, google_orders)

            total_gross_roas = safe_div(total_sales, total_ad_spent)
            total_net_roas = safe_div(total_sales - total_cogs, total_ad_spent)
            total_be_roas = safe_div(total_cogs + total_ad_spent, total_ad_spent)
            total_cpp = safe_div(total_ad_spent, total_orders)

            return {
                'meta': {
                    'sales': meta_sales,
                    'ad_spend': meta_ad_spend,
                    'cogs': meta_cogs,
                    'net_profit': meta_net_profit,
                    'gross_roas': round(meta_gross_roas, 2),
                    'net_roas': round(meta_net_roas, 2),
                    'be_roas': round(meta_be_roas, 2),
                    'quantity': meta_orders,
                    'cpp': round(meta_cpp, 2),
                    'order_count': meta_orders
                },
                'google': {
                    'sales': google_sales,
                    'ad_spend': google_ad_spend,
                    'cogs': google_cogs,
                    'net_profit': google_net_profit,
                    'gross_roas': round(google_gross_roas, 2),
                    'net_roas': round(google_net_roas, 2),
                    'be_roas': round(google_be_roas, 2),
                    'quantity': google_orders,
                    'cpp': round(google_cpp, 2),
                    'order_count': google_orders
                },
                'organic': {
                    'sales': organic_sales,
                    'ad_spend': 0,
                    'cogs': organic_cogs,
                    'net_profit': organic_net_profit,
                    'gross_roas': 0,
                    'net_roas': 0,
                    'be_roas': 0,
                    'quantity': organic_orders,
                    'cpp': 0,
                    'order_count': organic_orders
                },
                'total': {
                    'sales': total_sales,
                    'ad_spend': total_ad_spent,
                    'cogs': total_cogs,
                    'net_profit': total_net_profit,
                    'gross_roas': round(total_gross_roas, 2),
                    'net_roas': round(total_net_roas, 2),
                    'be_roas': round(total_be_roas, 2),
                    'quantity': total_orders,
                    'cpp': round(total_cpp, 2),
                    'order_count': total_orders
                }
            }

        # Fallback: original single-day implementation
        # Get combined UTM data (Meta only)
        from combinedUTMMeta import get_combined_utm_report_df
        combined_utm_df = get_combined_utm_report_df()
        
        if combined_utm_df.empty:
            logger.warning("No combined UTM data available, returning default values")
            return {
                'meta': {'sales': 0, 'ad_spend': 0, 'cogs': 0, 'net_profit': 0, 'gross_roas': 0, 'net_roas': 0, 'be_roas': 0, 'quantity': 0, 'cpp': 0, 'order_count': 0},
                'google': {'sales': 0, 'ad_spend': 0, 'cogs': 0, 'net_profit': 0, 'gross_roas': 0, 'net_roas': 0, 'be_roas': 0, 'quantity': 0, 'cpp': 0, 'order_count': 0},
                'organic': {'sales': 0, 'ad_spend': 0, 'cogs': 0, 'net_profit': 0, 'gross_roas': 0, 'net_roas': 0, 'be_roas': 0, 'quantity': 0, 'cpp': 0, 'order_count': 0},
                'total': {'sales': 0, 'ad_spend': 0, 'cogs': 0, 'net_profit': 0, 'gross_roas': 0, 'net_roas': 0, 'be_roas': 0, 'quantity': 0, 'cpp': 0, 'order_count': 0}
            }
        
        # Get the Total row for overall metrics (this only includes Meta data)
        total_row = combined_utm_df[combined_utm_df['ad_name'] == 'Total']
        if not total_row.empty:
            total_row = total_row.iloc[0]
            meta_total_sales = total_row['total_sales']
            meta_total_cogs = total_row['total_cogs']
            meta_total_ad_spent = total_row['ad_spent']
            meta_total_net_profit = total_row['net_profit']
            meta_total_orders = total_row['orders']
        else:
            # Calculate totals from all rows if no Total row exists
            meta_total_sales = combined_utm_df['total_sales'].sum()
            meta_total_cogs = combined_utm_df['total_cogs'].sum()
            meta_total_ad_spent = combined_utm_df['ad_spent'].sum()
            meta_total_net_profit = combined_utm_df['net_profit'].sum()
            meta_total_orders = combined_utm_df['orders'].sum()
        
        # Filter Meta campaigns (exclude Total and Additional Meta ad spent rows)
        # Since the combined UTM data only includes Meta campaigns, all non-summary rows are Meta
        meta_df = combined_utm_df[
            (combined_utm_df['ad_name'] != 'Total') & 
            (combined_utm_df['ad_name'] != 'Additional Meta ad spent')
        ]
        
        # Filter Google campaigns - since combined UTM data only includes Meta, this will be empty
        google_df = combined_utm_df[
            (combined_utm_df['ad_name'] != 'Total') & 
            (combined_utm_df['ad_name'] != 'Additional Meta ad spent') &
            False  # This will always be empty since combined UTM only has Meta data
        ]
        
        # Calculate Meta metrics from individual campaigns (excluding summary rows)
        meta_sales = meta_df['total_sales'].sum()
        meta_ad_spend = meta_df['ad_spent'].sum()
        meta_cogs = meta_df['total_cogs'].sum()
        meta_net_profit = meta_df['net_profit'].sum()
        meta_orders = meta_df['orders'].sum()
        
        # Calculate Google metrics (single day)
        from conversionTracking import get_google_metrics
        google_metrics = get_google_metrics()
        google_sales = google_metrics['total_sales']
        google_ad_spend = google_metrics['google_ad_spent']
        google_cogs = google_metrics['total_cogs']
        google_net_profit = google_metrics['net_profit']
        google_orders = google_metrics['total_orders']
        
        logger.info(f"Google metrics from get_google_metrics(): Sales={google_sales}, Ad Spend={google_ad_spend}, COGS={google_cogs}, Net Profit={google_net_profit}, Orders={google_orders}")
        
        # Calculate organic metrics (total - meta - google) for single day
        from conversionTracking import get_conversion_tracking_report_df
        conversion_df, _ = get_conversion_tracking_report_df()
        
        # Get total sales from all sources
        all_total_sales = conversion_df['total_sales'].sum() if not conversion_df.empty else 0
        all_total_cogs = conversion_df['total_cogs'].sum() if not conversion_df.empty else 0
        all_total_orders = conversion_df['orders'].sum() if not conversion_df.empty else 0
        
        # Calculate organic as the remainder
        organic_sales = all_total_sales - meta_sales - google_sales
        organic_ad_spend = 0  # Organic has no ad spend
        organic_cogs = all_total_cogs - meta_cogs - google_cogs
        organic_net_profit = organic_sales - organic_cogs  # No ad spend for organic
        organic_orders = all_total_orders - meta_orders - google_orders
        
        # Calculate true totals (sum of all sources)
        total_sales = meta_sales + google_sales + organic_sales
        total_cogs = meta_cogs + google_cogs + organic_cogs
        total_ad_spent = meta_ad_spend + google_ad_spend + organic_ad_spend
        total_net_profit = meta_net_profit + google_net_profit + organic_net_profit
        total_orders = meta_orders + google_orders + organic_orders
        
        logger.info(f"Calculation breakdown - All total sales: {all_total_sales}, Meta: {meta_sales}, Google: {google_sales}, Organic: {organic_sales}")
        logger.info(f"Calculation breakdown - All total orders: {all_total_orders}, Meta: {meta_orders}, Google: {google_orders}, Organic: {organic_orders}")
        
        # Calculate ROAS metrics
        meta_gross_roas = meta_sales / meta_ad_spend if meta_ad_spend > 0 else 0
        meta_net_roas = (meta_sales - meta_cogs) / meta_ad_spend if meta_ad_spend > 0 else 0
        meta_be_roas = (meta_cogs + meta_ad_spend) / meta_ad_spend if meta_ad_spend > 0 else 0
        meta_cpp = meta_ad_spend / meta_orders if meta_orders > 0 else 0
        
        # Google ROAS calculations using actual Google data
        google_gross_roas = google_sales / google_ad_spend if google_ad_spend > 0 else 0
        google_net_roas = (google_sales - google_cogs) / google_ad_spend if google_ad_spend > 0 else 0
        google_be_roas = (google_cogs + google_ad_spend) / google_ad_spend if google_ad_spend > 0 else 0
        google_cpp = google_ad_spend / google_orders if google_orders > 0 else 0
        
        total_gross_roas = total_sales / total_ad_spent if total_ad_spent > 0 else 0
        total_net_roas = (total_sales - total_cogs) / total_ad_spent if total_ad_spent > 0 else 0
        total_be_roas = (total_cogs + total_ad_spent) / total_ad_spent if total_ad_spent > 0 else 0
        total_cpp = total_ad_spent / total_orders if total_orders > 0 else 0
        
        # Log the final organized metrics for debugging
        logger.info(f"Final organized metrics - Meta: Sales={meta_sales}, Ad Spend={meta_ad_spend}, Orders={meta_orders}")
        logger.info(f"Final organized metrics - Google: Sales={google_sales}, Ad Spend={google_ad_spend}, Orders={google_orders}")
        logger.info(f"Final organized metrics - Organic: Sales={organic_sales}, Orders={organic_orders}")
        logger.info(f"Final organized metrics - Total: Sales={total_sales}, Ad Spend={total_ad_spent}, Orders={total_orders}")
        
        return {
            'meta': {
                'sales': meta_sales,
                'ad_spend': meta_ad_spend,
                'cogs': meta_cogs,
                'net_profit': meta_net_profit,
                'gross_roas': round(meta_gross_roas, 2),
                'net_roas': round(meta_net_roas, 2),
                'be_roas': round(meta_be_roas, 2),
                'quantity': meta_orders,
                'cpp': round(meta_cpp, 2),
                'order_count': meta_orders
            },
            'google': {
                'sales': google_sales,
                'ad_spend': google_ad_spend,
                'cogs': google_cogs,
                'net_profit': google_net_profit,
                'gross_roas': round(google_gross_roas, 2),
                'net_roas': round(google_net_roas, 2),
                'be_roas': round(google_be_roas, 2),
                'quantity': google_orders,
                'cpp': round(google_cpp, 2),
                'order_count': google_orders
            },
            'organic': {
                'sales': organic_sales,
                'ad_spend': organic_ad_spend,
                'cogs': organic_cogs,
                'net_profit': organic_net_profit,
                'gross_roas': 0,  # No ad spend for organic
                'net_roas': 0,    # No ad spend for organic
                'be_roas': 0,     # No ad spend for organic
                'quantity': organic_orders,
                'cpp': 0,         # No ad spend for organic
                'order_count': organic_orders
            },
            'total': {
                'sales': total_sales,
                'ad_spend': total_ad_spent,
                'cogs': total_cogs,
                'net_profit': total_net_profit,
                'gross_roas': round(total_gross_roas, 2),
                'net_roas': round(total_net_roas, 2),
                'be_roas': round(total_be_roas, 2),
                'quantity': total_orders,
                'cpp': round(total_cpp, 2),
                'order_count': total_orders
            }
        }
        
    except Exception as e:
        logger.error(f"Error getting organized metrics from UTM data: {str(e)}")
        # Return default values on error
        return {
            'meta': {'sales': 0, 'ad_spend': 0, 'cogs': 0, 'net_profit': 0, 'gross_roas': 0, 'net_roas': 0, 'be_roas': 0, 'quantity': 0, 'cpp': 0, 'order_count': 0},
            'google': {'sales': 0, 'ad_spend': 0, 'cogs': 0, 'net_profit': 0, 'gross_roas': 0, 'net_roas': 0, 'be_roas': 0, 'quantity': 0, 'cpp': 0, 'order_count': 0},
            'organic': {'sales': 0, 'ad_spend': 0, 'cogs': 0, 'net_profit': 0, 'gross_roas': 0, 'net_roas': 0, 'be_roas': 0, 'quantity': 0, 'cpp': 0, 'order_count': 0},
            'total': {'sales': 0, 'ad_spend': 0, 'cogs': 0, 'net_profit': 0, 'gross_roas': 0, 'net_roas': 0, 'be_roas': 0, 'quantity': 0, 'cpp': 0, 'order_count': 0}
        }

def main():
    try:
        # Single report day (default: today in IST). Locks globals so plots/rollup cannot
        # inherit a wider MTD window left by another script in the same process.
        timeframe = get_daily_report_timeframe()
        set_global_dates(timeframe['start_date'], timeframe['end_date'])
        report_day = timeframe['today']
        today, timestamp_str = report_day, timeframe['timestamp_str']
        from datetime import datetime
        if isinstance(today, str):
            today = datetime.strptime(today, "%Y-%m-%d")
        logger.info(
            "Starting daily export and report generation for %s (%s)",
            report_day,
            timestamp_str,
        )
        os.makedirs(REPORT_DIR, exist_ok=True)

        # Generate dailyrollup Excel for the timeframe and use its campaign data in PDF
        try:
            xlsx_path = run_dailyrollup(
                start_date=report_day,
                end_date=report_day,
                out_dir=REPORT_DIR,
            )
            logger.info(f"Dailyrollup Excel generated: {xlsx_path}")
        except Exception as e:
            logger.warning(f"Dailyrollup Excel generation failed: {e}")

        # Temporarily disabled — fetch_meta_data/process_data output is unused by PDF/email path.
        # data = fetch_meta_data(today)
        # metrics = process_data(data, today, timestamp_str)
        metrics = {"ad_level_report": None}

        # Generate the PDF report (ensure summary metrics respect timeframe range)
        pdf_file = generate_pdf_report(
            metrics,
            today,
            timestamp_str,
            timeframe_start=timeframe['start_date'],
            timeframe_end=timeframe['end_date']
        )

        # Generate plot files (if applicable)
        logger.info("Generating plots for email...")
        plot_files = generate_plots_for_email(
            days=30,
            report_dir=REPORT_DIR,
            report_day_str=timeframe["today"],
        )
        if plot_files:
            logger.info(f"Generated {len(plot_files)} plots for email")
        else:
            logger.warning("No plots were generated for email")

        # Hourly sales plot is already included by generate_plots_for_email (timestamped subdir)
        if plot_files:
            hourly_paths = [p for p in plot_files if p and 'hourly_sales_last_7_days' in p]
            if hourly_paths:
                logger.info(f"Hourly sales plot in attachments: {hourly_paths[0]}")

        # Prepare attachments list in the requested order:
        # 1. Marketing Report (PDF)
        # 2. Entity Report (dailyrollup xlsx) 
        # 3. Actionable Insight (ad recommendations)
        # 4. Activity History (meta activity)
        attachments = []

        # 1. Marketing Report (PDF) - First
        if pdf_file and os.path.exists(pdf_file):
            attachments.append(pdf_file)
            logger.info(f"Added marketing report (PDF) to attachments: {pdf_file}")

        # 2. Entity Report (dailyrollup xlsx) - Second
        try:
            if 'xlsx_path' in locals() and xlsx_path and os.path.exists(xlsx_path):
                attachments.append(xlsx_path)
                logger.info(f"Added entity report to attachments: {xlsx_path}")
            else:
                logger.warning(f"Entity report not found or not generated. xlsx_path: {xlsx_path if 'xlsx_path' in locals() else 'Not defined'}")
        except Exception as e:
            logger.error(f"Error adding entity report to attachments: {e}")
            pass

        # 3. Actionable Insight (ad recommendations) - Third
        _temp_dir = get_temp_dir()
        ad_recommendations_report_path = generate_ad_recommendations_report(output_dir=_temp_dir)
        if ad_recommendations_report_path and os.path.exists(ad_recommendations_report_path):
            attachments.append(ad_recommendations_report_path)
            logger.info(f"Added actionable insight (ad recommendations) to attachments: {ad_recommendations_report_path}")

        # 4. Activity History (meta activity) - Fourth
        meta_excel_path = generate_meta_activity_excel()
        if meta_excel_path and os.path.exists(meta_excel_path):
            attachments.append(meta_excel_path)
            logger.info(f"Added activity history (meta activity) to attachments: {meta_excel_path}")

        # Add plot files after the main reports
        if plot_files:
            attachments.extend(plot_files)
            logger.info(f"Added {len(plot_files)} plot files to attachments")

        # 5. Weather campaign opportunity (inline graph + CSV attachment)
        weather_bundle = None
        try:
            import sys
            _wr_src = os.path.join(os.path.dirname(__file__), "weather_report", "src")
            if _wr_src not in sys.path:
                sys.path.insert(0, _wr_src)
            from email_assets import build_weather_email_bundle

            weather_day_str = report_day
            logger.info("Fetching live weather campaign data for %s (DB + Open-Meteo)...", weather_day_str)
            weather_bundle = build_weather_email_bundle(
                weather_day_str, get_temp_dir(), top=15,
            )
            if weather_bundle:
                attachments.append(weather_bundle["csv_path"])
                attachments.append(weather_bundle["combined_plot"])
                if _daily_email_context is not None:
                    _daily_email_context["weather_report_date"] = weather_bundle.get("report_date", weather_day_str)
                    _daily_email_context["weather_report_time"] = weather_bundle.get("report_time", "")
                    _daily_email_context["weather_fetched_at"] = weather_bundle.get("weather_fetched_at", "")
                logger.info("Added weather report to email: %s", weather_bundle["csv_path"])
        except Exception as e:
            logger.warning("Weather report email section skipped: %s", e)

        # Send the email with attachments
        send_email(None, pdf_file, metrics['ad_level_report'], today, timestamp_str, plot_files, None, attachments)

        # Delete the meta activity excel file after sending the email
        if meta_excel_path and os.path.exists(meta_excel_path):
            try:
                os.remove(meta_excel_path)
                logger.info(f"Deleted meta activity excel file: {meta_excel_path}")
            except Exception as e:
                logger.warning(f"Failed to delete meta activity excel file {meta_excel_path}: {e}")
        # Delete the ad recommendations report after sending the email
        if ad_recommendations_report_path and os.path.exists(ad_recommendations_report_path):
            try:
                os.remove(ad_recommendations_report_path)
                logger.info(f"Deleted ad recommendations report: {ad_recommendations_report_path}")
            except Exception as e:
                logger.warning(f"Failed to delete ad recommendations report {ad_recommendations_report_path}: {e}")

        logger.info("Report generation completed and email sent successfully")
        
    except Exception as e:
        logger.error(f"Error in main function: {e}")
        raise
    finally:
        # Clean up database connections
        dispose_engine()

if __name__ == "__main__":
    main()
