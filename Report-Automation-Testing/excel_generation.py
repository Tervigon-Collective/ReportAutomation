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
from timeframe_config import get_timeframe_config, get_current_timestamp

from sqlalchemy.sql import text
from googleads import get_google_ads_total_spend, get_th_account_metrics
from metaActivityTrack import generate_meta_activity_excel
from database_manager import get_db_engine, dispose_engine
from ad_recommendations import generate_ad_recommendations_report
from api_data_fetcher import get_organized_metrics_for_pdf
from revenue_gst import apply_net_revenue_column
from dailyrollup import get_campaign_data, run as run_dailyrollup, get_campaign_grand_total_for_pdf, get_meta_funnel_metrics


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
            timeframe = get_timeframe_config(start_date, end_date, days_range=1, use_fixed_dates=True)
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

    # First, get active campaigns
    active_campaigns = fetch_active_campaigns(today)
    logger.info(f"Active campaigns found: {len(active_campaigns)}")

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

    # Get organized metrics from backend hourly APIs for the requested timeframe
    # Falls back to "today" if timeframe not provided
    api_metrics = get_organized_metrics_for_pdf(timeframe_start, timeframe_end)
    
    # Extract metrics for easier access
    meta_metrics = api_metrics['meta']
    google_metrics = api_metrics['google']
    organic_metrics = api_metrics['organic']
    total_metrics = api_metrics['total']
    
    try:
        c = canvas.Canvas(report_name, pagesize=letter)
        width, height = letter
        margin = 30  # Slightly increased margin for better breathing room
        table_width = width - 2 * margin
        y = height - margin
        
        # Professional color palette
        primary_color = (0.15, 0.15, 0.15)  # Dark charcoal for headers
        secondary_color = (0.4, 0.4, 0.4)  # Medium gray for subheaders
        accent_color = (0.1, 0.4, 0.7)  # Professional blue
        light_gray = (0.97, 0.97, 0.97)  # Very light gray for backgrounds
        border_color = (0.85, 0.85, 0.85)  # Subtle border color
        highlight_color = (0.95, 0.97, 1.0)  # Very light blue for highlights
        
        # Header section with enhanced typography
        c.setFont("Helvetica-Bold", 16)
        c.setFillColorRGB(*primary_color)
        # Display timeframe range if provided; otherwise, use single date
        if timeframe_start is not None and timeframe_end is not None:
            start_str = (timeframe_start.astimezone(IST) if getattr(timeframe_start, 'tzinfo', None) else IST.localize(timeframe_start)).strftime('%Y-%m-%d')
            end_str = (timeframe_end.astimezone(IST) if getattr(timeframe_end, 'tzinfo', None) else IST.localize(timeframe_end)).strftime('%Y-%m-%d')
            dt = f"{start_str} to {end_str}"
        else:
            dt = today.strftime('%Y-%m-%d') if isinstance(today, datetime) else str(today)
        time_part = datetime.now(IST).strftime('%H:%M')
        
        # Set title based on report type
        if report_type == 'wtd':
            title = f"Week-to-Date Marketing Performance Report"
        elif report_type == 'mtd':
            title = f"Month-to-Date Marketing Performance Report"
        else:
            title = f"Daily Marketing Performance Report"
        
        c.drawString(margin, y, title)
        
        # Subtitle with date and time - more elegant styling
        y -= 40
        c.setFont("Helvetica", 9)
        c.setFillColorRGB(*secondary_color)
        c.drawString(margin, y, f"{dt} • {time_part}")
        
        # Summary Metrics Section with enhanced styling
        y -= 50
        c.setFont("Helvetica-Bold", 12)
        c.setFillColorRGB(*primary_color)
        c.drawString(margin, y, "Summary Metrics")
        y -= 35
        
        # Create a comprehensive table with Labels, Meta, Google, Organic, and Total columns
        total_width = width - 2 * margin
        label_width = 85  # Slightly wider for better readability
        column_width = (total_width - label_width - 80) / 4  # 5 columns: Labels, Meta, Google, Organic, Total
        
        # Define column positions
        label_x = margin
        meta_x = margin + label_width + 20
        google_x = margin + label_width + 20 + column_width + 20
        organic_x = margin + label_width + 20 + 2 * (column_width + 20)
        total_x = margin + label_width + 20 + 3 * (column_width + 20)
        
        # Table headers with enhanced styling
        headers = ["", "Meta", "Google", "Organic", "Total"]
        header_x_positions = [label_x, meta_x, google_x, organic_x, total_x]
        
        # Enhanced header background with subtle gradient effect
        c.setFillColorRGB(*light_gray)
        c.rect(label_x, y - 8, label_width, 28, fill=True, stroke=False)
        for i, x in enumerate([meta_x, google_x, organic_x, total_x]):
            c.rect(x, y - 8, column_width, 28, fill=True, stroke=False)
        
        # Header text with better typography
        c.setFillColorRGB(*primary_color)
        c.setFont("Helvetica-Bold", 10)
        for i, header in enumerate(headers):
            if i == 0:  # Label column
                c.drawString(header_x_positions[i] + 8, y, header)
            else:  # Data columns
                c.drawString(header_x_positions[i] + 8, y, header)
        
        # Define table rows with proper labels and handle None values
        def safe_format(value, format_type='float'):
            """Safely format values, handling None and other edge cases"""
            if value is None:
                return "0.00" if format_type == 'float' else "0"
            try:
                if format_type == 'float':
                    return f"{float(value):.2f}"
                elif format_type == 'int':
                    return f"{int(value)}"
                else:
                    return str(value)
            except (ValueError, TypeError):
                return "0.00" if format_type == 'float' else "0"
        
        table_rows = [
            ("Sales", 
             f"Rs {safe_format(meta_metrics.get('sales'), 'float')}", 
             f"Rs {safe_format(google_metrics.get('sales'), 'float')}", 
             f"Rs {safe_format(organic_metrics.get('sales'), 'float')}", 
             f"Rs {safe_format(total_metrics.get('sales'), 'float')}"),
            ("Ad Spend", 
             f"Rs {safe_format(meta_metrics.get('ad_spend'), 'float')}", 
             f"Rs {safe_format(google_metrics.get('ad_spend'), 'float')}", 
             f"Rs {safe_format(organic_metrics.get('ad_spend'), 'float')}", 
             f"Rs {safe_format(total_metrics.get('ad_spend'), 'float')}"),
            ("COGS", 
             f"Rs {safe_format(meta_metrics.get('cogs'), 'float')}", 
             f"Rs {safe_format(google_metrics.get('cogs'), 'float')}", 
             f"Rs {safe_format(organic_metrics.get('cogs'), 'float')}", 
             f"Rs {safe_format(total_metrics.get('cogs'), 'float')}"),
            ("Net Profit", 
             f"Rs {safe_format(meta_metrics.get('net_profit'), 'float')}", 
             f"Rs {safe_format(google_metrics.get('net_profit'), 'float')}", 
             f"Rs {safe_format(organic_metrics.get('net_profit'), 'float')}", 
             f"Rs {safe_format(total_metrics.get('net_profit'), 'float')}"),
            ("Gross ROAS", 
             f"{safe_format(meta_metrics.get('gross_roas'), 'float')}", 
             f"{safe_format(google_metrics.get('gross_roas'), 'float')}", 
             f"{safe_format(organic_metrics.get('gross_roas'), 'float')}", 
             f"{safe_format(total_metrics.get('gross_roas'), 'float')}"),
            ("Net ROAS", 
             f"{safe_format(meta_metrics.get('net_roas'), 'float')}", 
             f"{safe_format(google_metrics.get('net_roas'), 'float')}", 
             f"{safe_format(organic_metrics.get('net_roas'), 'float')}", 
             f"{safe_format(total_metrics.get('net_roas'), 'float')}"),
            ("BE ROAS", 
             f"{safe_format(meta_metrics.get('be_roas'), 'float')}", 
             f"{safe_format(google_metrics.get('be_roas'), 'float')}", 
             f"{safe_format(organic_metrics.get('be_roas'), 'float')}", 
             f"{safe_format(total_metrics.get('be_roas'), 'float')}"),
            ("Total Quantity", 
             f"{safe_format(meta_metrics.get('quantity'), 'int')}", 
             f"{safe_format(google_metrics.get('quantity'), 'int')}", 
             f"{safe_format(organic_metrics.get('quantity'), 'int')}", 
             f"{safe_format(total_metrics.get('quantity'), 'int')}")
        ]
        c.setFont("Helvetica", 8)
        y -= 28
        
        # Draw table rows with enhanced styling
        for i, (row_label, meta_val, google_val, organic_val, total_val) in enumerate(table_rows):
            if y < margin + 30:
                c.showPage()
                y = height - margin
                y -= 30
                c.setFont("Helvetica", 8)
            
            # Enhanced row background with alternating colors for better readability
            if i % 2 == 0:
                c.setFillColorRGB(0.99, 0.99, 0.99)  # Very light background
            else:
                c.setFillColorRGB(0.95, 0.95, 0.95)  # Slightly darker background
            
            c.rect(label_x, y - 6, label_width, 24, fill=True, stroke=False)
            c.rect(meta_x, y - 6, column_width, 24, fill=True, stroke=False)
            c.rect(google_x, y - 6, column_width, 24, fill=True, stroke=False)
            c.rect(organic_x, y - 6, column_width, 24, fill=True, stroke=False)
            c.rect(total_x, y - 6, column_width, 24, fill=True, stroke=False)
            
            # Draw subtle borders
            c.setStrokeColorRGB(*border_color)
            c.rect(label_x, y - 6, label_width, 24, stroke=True, fill=False)
            c.rect(meta_x, y - 6, column_width, 24, stroke=True, fill=False)
            c.rect(google_x, y - 6, column_width, 24, stroke=True, fill=False)
            c.rect(organic_x, y - 6, column_width, 24, stroke=True, fill=False)
            c.rect(total_x, y - 6, column_width, 24, stroke=True, fill=False)
            
            # Draw labels and values with enhanced typography
            c.setFillColorRGB(*primary_color)
            c.setFont("Helvetica-Bold", 9)
            c.drawString(label_x + 8, y, row_label)
            c.setFont("Helvetica", 8)
            c.setFillColorRGB(0, 0, 0)
            c.drawString(meta_x + 8, y, meta_val)
            c.drawString(google_x + 8, y, google_val)
            c.drawString(organic_x + 8, y, organic_val)
            c.drawString(total_x + 8, y, total_val)
            
            y -= 24
        # Add a single line with total orders with enhanced styling
        y -= 25
        c.setFont("Helvetica-Bold", 10)
        c.setFillColorRGB(*primary_color)
        c.drawString(margin, y, f"Total Orders: {safe_format(total_metrics.get('order_count'), 'int')}")
        y -= 25
        
        # Add Performance Metrics section with funnel table
        y -= 25
        c.setFont("Helvetica-Bold", 12)
        c.setFillColorRGB(*primary_color)
        c.drawString(margin, y, "Performance Metrics - Funnel Analysis")
        y -= 35
        
        # Get Meta funnel metrics from dailyrollup
        try:
            # Use provided timeframe parameters if available, otherwise use timeframe_config
            if timeframe_start is not None and timeframe_end is not None:
                # Convert datetime to date strings for the functions
                start_date_str = timeframe_start.strftime('%Y-%m-%d') if hasattr(timeframe_start, 'strftime') else str(timeframe_start)
                end_date_str = timeframe_end.strftime('%Y-%m-%d') if hasattr(timeframe_end, 'strftime') else str(timeframe_end)
                funnel_metrics = get_meta_funnel_metrics(start_date=start_date_str, end_date=end_date_str)
                google_funnel_metrics = get_google_funnel_metrics(start_date=timeframe_start, end_date=timeframe_end)
            else:
                # Use global timeframe from timeframe_config
                funnel_metrics = get_meta_funnel_metrics()
                google_funnel_metrics = get_google_funnel_metrics()
            
            # Create side-by-side sections: Meta (left) and Google (right)
            
            # Calculate page width for two columns
            page_width = width - 2 * margin
            column_width = (page_width - 30) / 2  # 30px gap between columns
            
            # Meta Section (Left Column)
            meta_start_x = margin
            meta_end_x = margin + column_width
            
            # Google Section (Right Column)  
            google_start_x = margin + column_width + 30
            google_end_x = width - margin
            
            # Store the starting Y position for both sections
            section_start_y = y - 20
            
            # Meta Section Header
            c.setFont("Helvetica-Bold", 12)
            c.setFillColorRGB(*primary_color)
            c.drawString(meta_start_x, section_start_y, "Meta Funnel Metrics")
            
            # Google Section Header (same Y position)
            c.drawString(google_start_x, section_start_y, "Google Funnel Metrics")
            
            # Move down for table headers
            y = section_start_y - 25
            
            # Meta table headers
            meta_headers = ["Stage", "Value"]
            meta_stage_width = 120
            meta_value_width = 100
            meta_stage_x = meta_start_x
            meta_value_x = meta_start_x + meta_stage_width + 10
            
            # Google table headers (same Y position as Meta)
            google_stage_width = 120
            google_value_width = 100
            google_stage_x = google_start_x
            google_value_x = google_start_x + google_stage_width + 10
            
            # Meta header background
            c.setFillColorRGB(*light_gray)
            c.rect(meta_stage_x, y - 8, meta_stage_width, 28, fill=True, stroke=False)
            c.rect(meta_value_x, y - 8, meta_value_width, 28, fill=True, stroke=False)
            
            # Google header background (same Y position)
            c.rect(google_stage_x, y - 8, google_stage_width, 28, fill=True, stroke=False)
            c.rect(google_value_x, y - 8, google_value_width, 28, fill=True, stroke=False)
            
            # Meta header text
            c.setFillColorRGB(*primary_color)
            c.setFont("Helvetica-Bold", 10)
            c.drawString(meta_stage_x + 8, y, "Stage")
            c.drawString(meta_value_x + 8, y, "Value")
            
            # Google header text (same Y position)
            c.drawString(google_stage_x + 8, y, "Stage")
            c.drawString(google_value_x + 8, y, "Value")
            
            # Meta table rows
            meta_table_rows = [
                ("Impressions", f"{funnel_metrics.get('impressions', 0):,}"),
                ("Clicks", f"{funnel_metrics.get('clicks', 0):,} ({funnel_metrics.get('ctr', 0):.2f}%)"),
                ("Landing Page Views", f"{funnel_metrics.get('landing_page_views', 0):,} ({funnel_metrics.get('landing_page_rate', 0):.2f}%)"),
                ("Add to Cart", f"{funnel_metrics.get('add_to_cart', 0):,} ({funnel_metrics.get('add_to_cart_rate', 0):.2f}%)"),
                ("Orders", f"{funnel_metrics.get('orders', 0):,} ({funnel_metrics.get('conversion_rate', 0):.2f}%)")
            ]
            
            # Google table rows
            google_table_rows = [
                ("Clicks", f"{google_funnel_metrics.get('clicks', 0):,}"),
                ("CTR", f"{google_funnel_metrics.get('ctr', 0):.2f}%"),
                ("Interaction Rate", f"{google_funnel_metrics.get('interaction_rate', 0):.2f}%"),
                ("Orders", f"{google_funnel_metrics.get('orders', 0):,}")
            ]
            
            c.setFont("Helvetica", 8)
            y -= 28
            
            # Draw both tables side by side
            max_rows = max(len(meta_table_rows), len(google_table_rows))
            
            for i in range(max_rows):
                if y < margin + 30:
                    c.showPage()
                    y = height - margin
                    y -= 30
                    c.setFont("Helvetica", 8)
                
                # Enhanced row background with alternating colors
                if i % 2 == 0:
                    row_bg_color = (0.99, 0.99, 0.99)
                else:
                    row_bg_color = (0.95, 0.95, 0.95)
                
                # Draw Meta row
                if i < len(meta_table_rows):
                    stage, value = meta_table_rows[i]
                    
                    # Draw background
                    c.setFillColorRGB(*row_bg_color)
                    c.rect(meta_stage_x, y - 6, meta_stage_width, 24, fill=True, stroke=False)
                    c.rect(meta_value_x, y - 6, meta_value_width, 24, fill=True, stroke=False)
                    
                    # Draw borders
                    c.setStrokeColorRGB(*border_color)
                    c.rect(meta_stage_x, y - 6, meta_stage_width, 24, stroke=True, fill=False)
                    c.rect(meta_value_x, y - 6, meta_value_width, 24, stroke=True, fill=False)
                    
                    # Draw text - ensure proper color reset
                    c.setFillColorRGB(0, 0, 0)  # Black for stage names
                    c.setFont("Helvetica-Bold", 9)
                    c.drawString(meta_stage_x + 8, y, stage)
                    
                    # Reset fill color for value text
                    c.setFillColorRGB(0, 0, 0)  # Black for values
                    c.setFont("Helvetica", 8)
                    c.drawString(meta_value_x + 8, y, str(value))
                else:
                    # Draw empty Meta row
                    c.setFillColorRGB(*row_bg_color)
                    c.rect(meta_stage_x, y - 6, meta_stage_width, 24, fill=True, stroke=False)
                    c.rect(meta_value_x, y - 6, meta_value_width, 24, fill=True, stroke=False)
                    c.setStrokeColorRGB(*border_color)
                    c.rect(meta_stage_x, y - 6, meta_stage_width, 24, stroke=True, fill=False)
                    c.rect(meta_value_x, y - 6, meta_value_width, 24, stroke=True, fill=False)
                
                # Draw Google row
                if i < len(google_table_rows):
                    stage, value = google_table_rows[i]
                    
                    # Draw background
                    c.setFillColorRGB(*row_bg_color)
                    c.rect(google_stage_x, y - 6, google_stage_width, 24, fill=True, stroke=False)
                    c.rect(google_value_x, y - 6, google_value_width, 24, fill=True, stroke=False)
                    
                    # Draw borders
                    c.setStrokeColorRGB(*border_color)
                    c.rect(google_stage_x, y - 6, google_stage_width, 24, stroke=True, fill=False)
                    c.rect(google_value_x, y - 6, google_value_width, 24, stroke=True, fill=False)
                    
                    # Draw text - ensure proper color reset
                    c.setFillColorRGB(0, 0, 0)  # Black for stage names
                    c.setFont("Helvetica-Bold", 9)
                    c.drawString(google_stage_x + 8, y, stage)
                    
                    # Reset fill color for value text
                    c.setFillColorRGB(0, 0, 0)  # Black for values
                    c.setFont("Helvetica", 8)
                    c.drawString(google_value_x + 8, y, str(value))
                else:
                    # Draw empty Google row
                    c.setFillColorRGB(*row_bg_color)
                    c.rect(google_stage_x, y - 6, google_stage_width, 24, fill=True, stroke=False)
                    c.rect(google_value_x, y - 6, google_value_width, 24, fill=True, stroke=False)
                    c.setStrokeColorRGB(*border_color)
                    c.rect(google_stage_x, y - 6, google_stage_width, 24, stroke=True, fill=False)
                    c.rect(google_value_x, y - 6, google_value_width, 24, stroke=True, fill=False)
                
                y -= 24
            
            
            # Add drop-off analysis section for Meta only (positioned below both tables)
            final_y = y - 50
            
            c.setFont("Helvetica-Bold", 11)
            c.setFillColorRGB(*primary_color)
            c.drawString(margin, final_y, "Drop-off Analysis - Meta")
            final_y -= 20
            
            # Drop-off percentages for Meta
            meta_drop_off_stages = [
                ("Impressions → Clicks", f"{funnel_metrics.get('drop_off_impressions_to_clicks', 0):.1f}%"),
                ("Clicks → Landing Page Views", f"{funnel_metrics.get('drop_off_clicks_to_landing', 0):.1f}%"),
                ("Landing Page Views → Add to Cart", f"{funnel_metrics.get('drop_off_landing_to_cart', 0):.1f}%"),
                ("Add to Cart → Orders", f"{funnel_metrics.get('drop_off_cart_to_orders', 0):.1f}%")
            ]
            
            c.setFont("Helvetica", 9)
            for stage, drop_off in meta_drop_off_stages:
                if final_y < margin + 30:
                    c.showPage()
                    final_y = height - margin
                    final_y -= 30
                    c.setFont("Helvetica", 9)
                
                # Color code drop-off percentages
                drop_off_val = float(drop_off.replace('%', ''))
                if drop_off_val > 80:
                    c.setFillColorRGB(0.8, 0.2, 0.2)  # Red for high drop-off
                elif drop_off_val > 60:
                    c.setFillColorRGB(0.9, 0.6, 0.2)  # Orange for medium drop-off
                else:
                    c.setFillColorRGB(0.2, 0.6, 0.2)  # Green for low drop-off
                
                c.drawString(margin + 20, final_y, f"• {stage}: {drop_off}")
                final_y -= 16
            
            # Update main y position for next section
            y = final_y
            
        except Exception as e:
            # If funnel fails, show error message and continue
            c.setFont("Helvetica", 10)
            c.setFillColorRGB(0.8, 0.2, 0.2)
            c.drawString(margin, y, f"Error loading funnel metrics: {str(e)}")
            y -= 30
        
        def draw_wrapped_campaign_name(canvas, name, x, y, width, font_size=10):
            canvas.setFont("Helvetica", font_size)
            # For campaign names, split by hyphens and underscores for better wrapping
            words = name.replace('-', ' ').replace('_', ' ').split(' ')
            line = ""
            y_offset = 0
            max_lines = 3  # Allow up to 3 lines for very long campaign names
            line_height = font_size + 2  # Slightly more spacing between lines
            for word in words:
                test_line = line + " " + word if line else word
                if canvas.stringWidth(test_line, "Helvetica", font_size) < width - 10:
                    line = test_line
                else:
                    if y_offset < (max_lines - 1) * line_height:
                        if line:  # Only draw if we have content
                            canvas.drawString(x + 5, y - y_offset, line)
                        line = word
                        y_offset += line_height
                    else:
                        # Last line - truncate if necessary
                        remaining = line + " " + word
                        while canvas.stringWidth(remaining + "...", "Helvetica", font_size) > width - 10 and len(remaining) > 0:
                            remaining = remaining[:-1]
                        canvas.drawString(x + 5, y - y_offset, remaining + "..." if len(remaining) < len(line + " " + word) else remaining)
                        return
            if line:
                canvas.drawString(x + 5, y - y_offset, line)
        y -= 15

        # --- Regular Campaigns Table: Segmented by Net ROAS (minimal styling) ---
        y -= 35
        c.setFont("Helvetica-Bold", 11)
        c.setFillColorRGB(*primary_color)
        c.drawString(margin, y, "Campaigns by Net ROAS Segments")
        y -= 25
        headers = [
            "Campaign Name", "Spend", "Revenue", "Net Profit", "CTR", "Bounce", "CR",
            "GR", "NR"
        ]
        # Slightly tightened widths for compact look
        col_widths = [150, 55, 65, 70, 38, 46, 46, 46, 46]
        table_width = sum(col_widths)
        table_margin = (width - table_width) / 2
        x_positions = [table_margin]
        for i in range(len(col_widths) - 1):
            x_positions.append(x_positions[i] + col_widths[i])
        
        # Draw the table header
        c.setFillColorRGB(*light_gray)
        c.rect(table_margin, y - 6, table_width, 22, fill=True, stroke=False)
        c.setFillColorRGB(*primary_color)
        c.setFont("Helvetica-Bold", 9)
        for i, header in enumerate(headers):
            c.drawString(x_positions[i] + 6, y, header)
        y -= 22

        # Helper: draw right-aligned text in the totals rows (shared)
        def rtot(ix, text):
            c.drawRightString(x_positions[ix] + col_widths[ix] - 8, y, text)

        # Get campaign_summary from metrics and normalize expected columns for PDF
        campaign_df = metrics['campaign_summary']
        
        logger.info(f"Campaign summary rows: {len(campaign_df)}")
        
        try:
            if not campaign_df.empty:
                # Ensure expected columns by aliasing/computing
                if 'shopify_revenue' not in campaign_df.columns and 'sales' in campaign_df.columns:
                    campaign_df['shopify_revenue'] = campaign_df['sales']
                # sales
                if 'sales' not in campaign_df.columns:
                    if 'shopify_revenue' in campaign_df.columns:
                        campaign_df['sales'] = campaign_df['shopify_revenue']
                    elif 'revenue' in campaign_df.columns:
                        campaign_df['sales'] = campaign_df['revenue']
                    else:
                        campaign_df['sales'] = 0
                # purchases
                if 'purchases' not in campaign_df.columns:
                    if 'shopify_orders' in campaign_df.columns:
                        campaign_df['purchases'] = campaign_df['shopify_orders']
                    elif 'orders' in campaign_df.columns:
                        campaign_df['purchases'] = campaign_df['orders']
                    else:
                        campaign_df['purchases'] = 0
                # roas (gross)
                if 'roas' not in campaign_df.columns and 'gross_roas' in campaign_df.columns:
                    campaign_df['roas'] = campaign_df['gross_roas']
                if 'roas' not in campaign_df.columns:
                    with np.errstate(divide='ignore', invalid='ignore'):
                        campaign_df['roas'] = (pd.to_numeric(campaign_df.get('sales', 0), errors='coerce') / pd.to_numeric(campaign_df.get('spend', 0), errors='coerce'))
                    campaign_df['roas'] = campaign_df['roas'].replace([np.inf, -np.inf], 0).fillna(0)
                # net_roas
                if 'net_roas' not in campaign_df.columns and {'sales','cogs','spend'}.issubset(campaign_df.columns):
                    with np.errstate(divide='ignore', invalid='ignore'):
                        campaign_df['net_roas'] = (pd.to_numeric(campaign_df['sales'], errors='coerce') - pd.to_numeric(campaign_df.get('cogs', 0), errors='coerce')) / pd.to_numeric(campaign_df['spend'], errors='coerce')
                    campaign_df['net_roas'] = campaign_df['net_roas'].replace([np.inf, -np.inf], 0).fillna(0)
                # be_roas / breakeven_roas
                if 'be_roas' not in campaign_df.columns and 'breakeven_roas' in campaign_df.columns:
                    campaign_df['be_roas'] = campaign_df['breakeven_roas']
                if 'breakeven_roas' not in campaign_df.columns:
                    if 'be_roas' in campaign_df.columns:
                        campaign_df['breakeven_roas'] = campaign_df['be_roas']
                    elif {'cogs','spend'}.issubset(campaign_df.columns):
                        with np.errstate(divide='ignore', invalid='ignore'):
                            campaign_df['breakeven_roas'] = (pd.to_numeric(campaign_df['cogs'], errors='coerce') + pd.to_numeric(campaign_df['spend'], errors='coerce')) / pd.to_numeric(campaign_df['spend'], errors='coerce')
                        campaign_df['breakeven_roas'] = campaign_df['breakeven_roas'].replace([np.inf, -np.inf], 0).fillna(0)
                    else:
                        campaign_df['breakeven_roas'] = 0
                if 'be_roas' not in campaign_df.columns and 'breakeven_roas' in campaign_df.columns:
                    campaign_df['be_roas'] = campaign_df['breakeven_roas']
                # cpp
                if 'cpp' not in campaign_df.columns and {'spend','purchases'}.issubset(campaign_df.columns):
                    with np.errstate(divide='ignore', invalid='ignore'):
                        campaign_df['cpp'] = pd.to_numeric(campaign_df['spend'], errors='coerce') / pd.to_numeric(campaign_df['purchases'], errors='coerce')
                    campaign_df['cpp'] = campaign_df['cpp'].replace([np.inf, -np.inf], 0).fillna(0)
                if 'cpp' not in campaign_df.columns:
                    campaign_df['cpp'] = 0
                # Ensure impressions exist if ctr is present but impressions missing
                if 'impressions' not in campaign_df.columns and 'ctr' in campaign_df.columns and 'clicks' in campaign_df.columns:
                    try:
                        clicks_num = pd.to_numeric(campaign_df['clicks'], errors='coerce')
                        ctr_num = pd.to_numeric(campaign_df['ctr'], errors='coerce')
                        # impressions = clicks / (ctr/100) when ctr > 0
                        campaign_df['impressions'] = np.where(ctr_num > 0, (clicks_num / (ctr_num / 100.0)), np.nan)
                    except Exception:
                        campaign_df['impressions'] = np.nan
                # ctr: compute when missing or zero but we have clicks+impressions
                if 'ctr' not in campaign_df.columns or (pd.to_numeric(campaign_df.get('ctr', 0), errors='coerce').fillna(0) == 0).all():
                    if {'clicks','impressions'}.issubset(campaign_df.columns):
                        with np.errstate(divide='ignore', invalid='ignore'):
                            campaign_df['ctr'] = (pd.to_numeric(campaign_df['clicks'], errors='coerce') / pd.to_numeric(campaign_df['impressions'], errors='coerce')) * 100
                        campaign_df['ctr'] = campaign_df['ctr'].replace([np.inf, -np.inf], 0).fillna(0)
                    else:
                        campaign_df['ctr'] = campaign_df.get('ctr', 0).fillna(0) if 'ctr' in campaign_df.columns else 0
                # conversion_rate: only recalculate if missing or invalid; preserve dailyrollup values
                if 'conversion_rate' not in campaign_df.columns or campaign_df['conversion_rate'].isna().all() or (campaign_df['conversion_rate'] == 0).all():
                    # Only recalculate if conversion_rate is missing or all zeros
                    if 'clicks' in campaign_df.columns:
                        clicks_series = pd.to_numeric(campaign_df['clicks'], errors='coerce').fillna(0)
                    else:
                        clicks_series = pd.Series([0]*len(campaign_df))
                    if (clicks_series == 0).any():
                        try:
                            if 'impressions' in campaign_df.columns and 'ctr' in campaign_df.columns:
                                imp_num = pd.to_numeric(campaign_df['impressions'], errors='coerce').fillna(0)
                                ctr_num = pd.to_numeric(campaign_df['ctr'], errors='coerce').fillna(0)
                                inferred_clicks = (imp_num * (ctr_num / 100.0)).round()
                                clicks_series = np.where(clicks_series == 0, inferred_clicks, clicks_series)
                                campaign_df['clicks'] = clicks_series
                        except Exception:
                            pass
                    if 'purchases' in campaign_df.columns:
                        with np.errstate(divide='ignore', invalid='ignore'):
                            campaign_df['conversion_rate'] = (pd.to_numeric(campaign_df['purchases'], errors='coerce') / pd.to_numeric(campaign_df['clicks'], errors='coerce')) * 100
                        campaign_df['conversion_rate'] = campaign_df['conversion_rate'].replace([np.inf, -np.inf], 0).fillna(0)
                    else:
                        campaign_df['conversion_rate'] = 0
                else:
                    # Ensure conversion_rate is properly formatted (already calculated by dailyrollup)
                    campaign_df['conversion_rate'] = pd.to_numeric(campaign_df['conversion_rate'], errors='coerce').fillna(0)
                
                pass
                        
        except Exception as e:
            logger.warning(f"Campaign DataFrame normalization issue: {e}")
            logger.error("Normalization exception details: %s", str(e), exc_info=True)
        
        # Check if campaign_df is empty or doesn't have required columns
        if campaign_df.empty or 'campaign_name' not in campaign_df.columns:
            logger.warning(f"Campaign DataFrame is empty or missing required columns. DataFrame shape: {campaign_df.shape}, columns: {list(campaign_df.columns)}. Creating empty campaign sections.")
            
            # Create a placeholder row to show "No data available" message
            placeholder_data = {
                'campaign_name': ['No campaign data available'],
                'spend': [0.0],
                'shopify_revenue': [0.0],
                'sales': [0.0],
                'purchases': [0],
                'orders': [0],
                'clicks': [0],
                'impressions': [0],
                'ctr': [0.0],
                'bounce_rate': [0.0],
                'conversion_rate': [0.0],
                'gross_roas': [0.0],
                'roas': [0.0],
                'net_roas': [0.0],
                'net_profit': [0.0],
                'cogs': [0.0],
                'be_roas': [0.0],
                'breakeven_roas': [0.0],
                'cpp': [0.0]
            }
            campaign_df = pd.DataFrame(placeholder_data)
            logger.info("Created placeholder campaign data for empty campaign summary")
            
            pmf_campaigns = pd.DataFrame()
            regular_campaigns = campaign_df  # Use placeholder for regular campaigns
            seg1 = pd.DataFrame()
            seg2 = pd.DataFrame()
            seg3 = campaign_df  # Show placeholder in segment 3
        else:
            pmf_campaigns = campaign_df[campaign_df['campaign_name'].str.contains('PMF', case=False, na=False)]
            
            logger.info("PMF campaigns: %s", len(pmf_campaigns))
            
            # Sort PMF campaigns by net ROAS (descending)
            if not pmf_campaigns.empty:
                pmf_campaigns = pmf_campaigns.sort_values('net_roas', ascending=False)
            
            # Filter out PMF campaigns for regular campaigns section
            regular_campaigns = campaign_df[~campaign_df['campaign_name'].str.contains('PMF', case=False, na=False)]
            
            logger.info("Regular campaigns: %s", len(regular_campaigns))
            
            # Filter out rows with empty campaign names (summary rows)
            regular_campaigns = regular_campaigns[regular_campaigns['campaign_name'].notna() & (regular_campaigns['campaign_name'].str.strip() != '')]
            
            logger.info("Campaigns after name filter: %s", len(regular_campaigns))
            
            # Segment regular campaigns
            seg1 = regular_campaigns[regular_campaigns['net_roas'] > 1]
            seg2 = regular_campaigns[(regular_campaigns['net_roas'] <= 1) & (regular_campaigns['net_roas'] > 0.8)]
            seg3 = regular_campaigns[regular_campaigns['net_roas'] <= 0.8]
            
            logger.info(
                "Campaign segments -> S1:%s S2:%s S3:%s",
                len(seg1), len(seg2), len(seg3)
            )
        
        def draw_campaign_rows(df, y):
            # Check if DataFrame has required columns
            required_columns = [
                'campaign_name', 'spend', 'shopify_revenue', 'ctr', 'bounce_rate',
                'gross_roas', 'net_roas', 'conversion_rate', 'net_profit'
            ]
            missing_columns = [col for col in required_columns if col not in df.columns]
            
            if missing_columns:
                logger.warning(f"Missing required columns in campaign DataFrame: {missing_columns}")
                return y
            # Minimal row height and light borders
            border_rgb = border_color
            c.setStrokeColorRGB(*border_rgb)
            
            for _, row in df.iterrows():
                if y < margin + 30:
                    c.showPage()
                    y = height - margin
                    # Header redraw on new page
                    c.setFillColorRGB(*light_gray)
                    c.rect(table_margin, y - 6, table_width, 22, fill=True, stroke=False)
                    c.setFillColorRGB(*primary_color)
                    c.setFont("Helvetica-Bold", 9)
                    for i, header in enumerate(headers):
                        c.drawString(x_positions[i] + 6, y, header)
                    y -= 22
                row_height = 32  # Increased height to accommodate wrapped campaign names
                # Gradient yellow intensity for CTR cell based on value (0-100)
                try:
                    ctr_val = float(row.get('ctr', 0) or 0)
                    if ctr_val > 0:
                        t = max(0.0, min(100.0, ctr_val)) / 100.0
                        start_r, start_g, start_b = (1.00, 1.00, 0.92)
                        end_r, end_g, end_b = (1.00, 0.90, 0.10)
                        r = start_r + (end_r - start_r) * t
                        g = start_g + (end_g - start_g) * t
                        b = start_b + (end_b - start_b) * t
                        c.setFillColorRGB(r, g, b)
                        c.rect(x_positions[4], y - row_height + 14, col_widths[4], row_height, fill=True, stroke=False)
                        c.setFillColorRGB(0, 0, 0)
                except Exception:
                    pass

                # Gradient blue intensity for Bounce Rate cell based on value (0-100)
                try:
                    bounce_val = float(row.get('bounce_rate', 0) or 0)
                    if bounce_val > 0:
                        # Normalize to [0,1]
                        t = max(0.0, min(100.0, bounce_val)) / 100.0
                        # From very light blue to professional blue
                        start_r, start_g, start_b = (0.95, 0.97, 1.0)
                        end_r, end_g, end_b = (0.10, 0.40, 0.70)
                        r = start_r + (end_r - start_r) * t
                        g = start_g + (end_g - start_g) * t
                        b = start_b + (end_b - start_b) * t
                        c.setFillColorRGB(r, g, b)
                        c.rect(x_positions[5], y - row_height + 14, col_widths[5], row_height, fill=True, stroke=False)
                        c.setFillColorRGB(0, 0, 0)
                except Exception:
                    pass


                # Red background for Net ROAS < 1
                try:
                    net_roas_val = float(row.get('net_roas', 0) or 0)
                    if net_roas_val < 1 and net_roas_val > 0:  # Only highlight if > 0 to avoid highlighting 0 values
                        c.setFillColorRGB(1.0, 0.78, 0.81)  # Light red background
                        c.rect(x_positions[8], y - row_height + 14, col_widths[8], row_height, fill=True, stroke=False)
                        c.setFillColorRGB(0, 0, 0)
                except Exception:
                    pass
                for i in range(len(col_widths)):
                    c.rect(x_positions[i], y - row_height + 14, col_widths[i], row_height, stroke=True, fill=False)
                # Text styles
                c.setFont("Helvetica", 8)
                # Campaign name (left aligned) - increased font size for better readability
                draw_wrapped_campaign_name(c, row['campaign_name'], x_positions[0], y, col_widths[0], font_size=9)
                # Right align numeric columns for neatness
                def rtext(ix, text):
                    c.drawRightString(x_positions[ix] + col_widths[ix] - 6, y, text)
                def safe_get(row, key, default=0):
                    val = row.get(key, default)
                    if hasattr(val, 'iloc'):
                        return val.iloc[0] if len(val) > 0 else default
                    return val
                
                rtext(1, f"Rs {safe_get(row, 'spend'):.2f}")
                rtext(2, f"Rs {safe_get(row, 'shopify_revenue'):.2f}")
                rtext(3, f"Rs {safe_get(row, 'net_profit'):.2f}")
                rtext(4, f"{safe_get(row, 'ctr'):.2f}%")
                rtext(5, f"{safe_get(row, 'bounce_rate'):.2f}%")
                rtext(6, f"{safe_get(row, 'conversion_rate'):.2f}%")
                rtext(7, f"{safe_get(row, 'gross_roas', safe_get(row, 'roas')):.2f}")
                rtext(8, f"{safe_get(row, 'net_roas'):.2f}")
                y -= row_height
            return y
        
        # Calculate overall totals from displayed campaigns for percentage calculations
        displayed_campaigns = pd.DataFrame()
        
        # Add campaigns from each section that was actually drawn
        if not seg1.empty:
            displayed_campaigns = pd.concat([displayed_campaigns, seg1], ignore_index=True)
        if not seg2.empty:
            displayed_campaigns = pd.concat([displayed_campaigns, seg2], ignore_index=True)
        if not seg3.empty:
            displayed_campaigns = pd.concat([displayed_campaigns, seg3], ignore_index=True)
        if not pmf_campaigns.empty:
            displayed_campaigns = pd.concat([displayed_campaigns, pmf_campaigns], ignore_index=True)
        
        # Compute totals directly from displayed campaigns
        if displayed_campaigns.empty or not all(col in displayed_campaigns.columns for col in ['spend', 'shopify_revenue', 'purchases']):
            logger.warning("Displayed campaigns DataFrame is empty or missing required columns. Using default values.")
            total_spend = 0.00
            total_sales = 0.00
            total_conversions = 0
            total_cpp = 0.00
            total_gross_roas = 0.00
            total_net_roas = 0.00
            total_be_roas = 0.00
            total_ctr = 0.00
            total_cr = 0.00
            total_bounce = 0.00
            total_checkout = 0.00
        else:
            total_spend = float(displayed_campaigns['spend'].sum())
            total_sales = float(displayed_campaigns['shopify_revenue'].sum())
            total_conversions = float(displayed_campaigns['purchases'].sum()) if 'purchases' in displayed_campaigns.columns else 0.0
            total_cogs = float(displayed_campaigns['cogs'].sum()) if 'cogs' in displayed_campaigns.columns else 0.0
            total_net_profit = total_sales - total_cogs - total_spend
            total_clicks = float(displayed_campaigns['clicks'].sum()) if 'clicks' in displayed_campaigns.columns else 0.0
            total_impressions = float(displayed_campaigns['impressions'].sum()) if 'impressions' in displayed_campaigns.columns else 0.0
            # CPP
            total_cpp = round(total_spend / total_conversions, 2) if total_conversions > 0 else 0.00
            # ROAS from totals
            total_gross_roas = round(total_sales / total_spend, 2) if total_spend > 0 else 0.00
            total_net_roas = round((total_sales - total_cogs) / total_spend, 2) if total_spend > 0 else 0.00
            total_be_roas = round((total_cogs + total_spend) / total_spend, 2) if total_spend > 0 else 0.00
            # CTR and CR from totals
            total_ctr = round((total_clicks / total_impressions) * 100, 2) if total_impressions > 0 else 0.00
            total_cr = round((total_conversions / total_clicks) * 100, 2) if total_clicks > 0 else 0.00
            # Weighted bounce by clicks; fallback to mean
            try:
                if 'bounce_rate' in displayed_campaigns.columns and 'clicks' in displayed_campaigns.columns and displayed_campaigns['clicks'].sum() > 0:
                    total_bounce = round(np.average(displayed_campaigns['bounce_rate'], weights=displayed_campaigns['clicks']), 2)
                else:
                    total_bounce = round(displayed_campaigns['bounce_rate'].mean(), 2) if 'bounce_rate' in displayed_campaigns.columns else 0.00
            except Exception:
                total_bounce = round(displayed_campaigns['bounce_rate'].mean(), 2) if 'bounce_rate' in displayed_campaigns.columns else 0.00
            total_checkout = 0.00
        
        # Only draw sections if there are campaigns in them
        sections_drawn = False
        
        # Section 1: Net ROAS > 1
        if not seg1.empty:
            sections_drawn = True
            c.setFont("Helvetica-Bold", 11)
            y-= 20
            c.setFillColorRGB(*primary_color)
            c.drawString(table_margin, y, "Net ROAS > 1")
            y -= 20
            y = draw_campaign_rows(seg1, y)
            # Section 1 Total Row with enhanced styling (sum/average per new schema)
            seg1_total_spend = float(seg1.get('spend', 0).sum()) if 'spend' in seg1.columns else 0.0
            if 'shopify_revenue' in seg1.columns:
                seg1_total_sales = float(seg1['shopify_revenue'].sum())
            elif 'sales' in seg1.columns:
                seg1_total_sales = float(seg1['sales'].sum())
            else:
                seg1_total_sales = 0.0
            seg1_total_cogs = float(seg1.get('cogs', 0).sum()) if 'cogs' in seg1.columns else 0.0
            seg1_total_net_profit = seg1_total_sales - seg1_total_cogs - seg1_total_spend
            # CTR/CR from totals for consistency
            seg1_clicks_tot = float(seg1.get('clicks', 0).sum()) if 'clicks' in seg1.columns else 0.0
            seg1_impr_tot = float(seg1.get('impressions', 0).sum()) if 'impressions' in seg1.columns else 0.0
            seg1_avg_ctr = (seg1_clicks_tot / seg1_impr_tot * 100.0) if seg1_impr_tot > 0 else (float(seg1.get('ctr', 0).mean()) if 'ctr' in seg1.columns else 0.0)
            seg1_avg_bounce = float(seg1.get('bounce_rate', 0).mean()) if 'bounce_rate' in seg1.columns else 0.0
            seg1_avg_checkout = 0.0
            # ROAS: prefer recompute from sums when possible
            seg1_avg_gross_roas = (seg1_total_sales / seg1_total_spend) if seg1_total_spend > 0 else (float(seg1.get('gross_roas', 0).mean()) if 'gross_roas' in seg1.columns else float(seg1.get('roas', 0).mean()) if 'roas' in seg1.columns else 0.0)
            seg1_avg_net_roas = ((seg1_total_sales - seg1_total_cogs) / seg1_total_spend) if seg1_total_spend > 0 and 'cogs' in seg1.columns else (float(seg1.get('net_roas', 0).mean()) if 'net_roas' in seg1.columns else 0.0)
            seg1_avg_be_roas = ((seg1_total_cogs + seg1_total_spend) / seg1_total_spend) if seg1_total_spend > 0 and 'cogs' in seg1.columns else (float(seg1.get('be_roas', 0).mean()) if 'be_roas' in seg1.columns else float(seg1.get('breakeven_roas', 0).mean()) if 'breakeven_roas' in seg1.columns else 0.0)
            # Calculate average conversion rate from individual campaigns in section 1
            seg1_avg_cr = float(seg1.get('conversion_rate', 0).mean()) if 'conversion_rate' in seg1.columns else 0.0
            logger.info(f"Section 1 CR Debug: Individual CRs={seg1.get('conversion_rate', []).tolist()}, Average CR={seg1_avg_cr:.2f}%")
            c.setFillColorRGB(*light_gray)
            c.rect(table_margin, y - 6, table_width, 22, fill=True, stroke=False)
            c.setFillColorRGB(*primary_color)
            c.setFont("Helvetica-Bold", 9)
            c.drawString(x_positions[0] + 8, y, "Section Total")
            
            # Calculate percentages for spend and sales
            spend_percentage = round((seg1_total_spend / total_spend * 100), 1) if total_spend > 0 else 0.0
            sales_percentage = round((seg1_total_sales / total_sales * 100), 1) if total_sales > 0 else 0.0
            
            def rtot(ix, text):
                c.drawRightString(x_positions[ix] + col_widths[ix] - 8, y, text)
            # First line: amounts only
            rtot(1, f"Rs {seg1_total_spend:.2f}")
            rtot(2, f"Rs {seg1_total_sales:.2f}")
            rtot(3, f"Rs {seg1_total_net_profit:.2f}")
            rtot(4, f"{seg1_avg_ctr:.2f}%")
            rtot(5, f"{seg1_avg_bounce:.2f}%")
            rtot(6, f"{seg1_avg_cr:.2f}%")
            rtot(7, f"{seg1_avg_gross_roas:.2f}")
            rtot(8, f"{seg1_avg_net_roas:.2f}")
            for i in range(len(col_widths)):
                c.rect(x_positions[i], y - 6, col_widths[i], 22, stroke=True, fill=False)
            # Second line: percentages under Spend and Revenue
            try:
                c.setFont("Helvetica", 7)
                c.setFillColorRGB(0, 0, 0)
                c.drawRightString(x_positions[1] + col_widths[1] - 8, y - 14, f"({spend_percentage}%)")
                c.drawRightString(x_positions[2] + col_widths[2] - 8, y - 14, f"({sales_percentage}%)")
            except Exception:
                pass
            y -= 24
        
        # Section 2: Net ROAS <= 1 and > 0.8
        if not seg2.empty:
            sections_drawn = True
            c.setFont("Helvetica-Bold", 11)
            y-= 20
            c.setFillColorRGB(*primary_color)
            c.drawString(table_margin, y, "Net ROAS <= 1 and > 0.8")
            y -= 20
            y = draw_campaign_rows(seg2, y)
            # Section 2 Total Row (sum/average per new schema)
            seg2_total_spend = float(seg2.get('spend', 0).sum()) if 'spend' in seg2.columns else 0.0
            if 'shopify_revenue' in seg2.columns:
                seg2_total_sales = float(seg2['shopify_revenue'].sum())
            elif 'sales' in seg2.columns:
                seg2_total_sales = float(seg2['sales'].sum())
            else:
                seg2_total_sales = 0.0
            seg2_total_cogs = float(seg2.get('cogs', 0).sum()) if 'cogs' in seg2.columns else 0.0
            seg2_total_net_profit = seg2_total_sales - seg2_total_cogs - seg2_total_spend
            seg2_clicks_tot = float(seg2.get('clicks', 0).sum()) if 'clicks' in seg2.columns else 0.0
            seg2_impr_tot = float(seg2.get('impressions', 0).sum()) if 'impressions' in seg2.columns else 0.0
            seg2_avg_ctr = (seg2_clicks_tot / seg2_impr_tot * 100.0) if seg2_impr_tot > 0 else (float(seg2.get('ctr', 0).mean()) if 'ctr' in seg2.columns else 0.0)
            seg2_avg_bounce = float(seg2.get('bounce_rate', 0).mean()) if 'bounce_rate' in seg2.columns else 0.0
            seg2_avg_checkout = 0.0
            seg2_avg_gross_roas = (seg2_total_sales / seg2_total_spend) if seg2_total_spend > 0 else (float(seg2.get('gross_roas', 0).mean()) if 'gross_roas' in seg2.columns else float(seg2.get('roas', 0).mean()) if 'roas' in seg2.columns else 0.0)
            seg2_avg_net_roas = ((seg2_total_sales - seg2_total_cogs) / seg2_total_spend) if seg2_total_spend > 0 and 'cogs' in seg2.columns else (float(seg2.get('net_roas', 0).mean()) if 'net_roas' in seg2.columns else 0.0)
            seg2_avg_be_roas = ((seg2_total_cogs + seg2_total_spend) / seg2_total_spend) if seg2_total_spend > 0 and 'cogs' in seg2.columns else (float(seg2.get('be_roas', 0).mean()) if 'be_roas' in seg2.columns else float(seg2.get('breakeven_roas', 0).mean()) if 'breakeven_roas' in seg2.columns else 0.0)
            # Calculate average conversion rate from individual campaigns in section 2
            seg2_avg_cr = float(seg2.get('conversion_rate', 0).mean()) if 'conversion_rate' in seg2.columns else 0.0
            logger.info(f"Section 2 CR Debug: Individual CRs={seg2.get('conversion_rate', []).tolist()}, Average CR={seg2_avg_cr:.2f}%")
            c.setFillColorRGB(*light_gray)
            c.rect(table_margin, y - 6, table_width, 22, fill=True, stroke=False)
            c.setFillColorRGB(*primary_color)
            c.setFont("Helvetica-Bold", 9)
            c.drawString(x_positions[0] + 8, y, "Section Total")
            
            # Calculate percentages for spend and sales
            spend_percentage = round((seg2_total_spend / total_spend * 100), 1) if total_spend > 0 else 0.0
            sales_percentage = round((seg2_total_sales / total_sales * 100), 1) if total_sales > 0 else 0.0
            
            # First line: amounts only
            rtot(1, f"Rs {seg2_total_spend:.2f}")
            rtot(2, f"Rs {seg2_total_sales:.2f}")
            rtot(3, f"Rs {seg2_total_net_profit:.2f}")
            rtot(4, f"{seg2_avg_ctr:.2f}%")
            rtot(5, f"{seg2_avg_bounce:.2f}%")
            rtot(6, f"{seg2_avg_cr:.2f}%")
            rtot(7, f"{seg2_avg_gross_roas:.2f}")
            rtot(8, f"{seg2_avg_net_roas:.2f}")
            for i in range(len(col_widths)):
                c.rect(x_positions[i], y - 6, col_widths[i], 22, stroke=True, fill=False)
            # Second line: percentages under Spend and Revenue
            try:
                c.setFont("Helvetica", 7)
                c.setFillColorRGB(0, 0, 0)
                c.drawRightString(x_positions[1] + col_widths[1] - 8, y - 14, f"({spend_percentage}%)")
                c.drawRightString(x_positions[2] + col_widths[2] - 8, y - 14, f"({sales_percentage}%)")
            except Exception:
                pass
            y -= 24
        
        # Section 3: Net ROAS <= 0.8
        if not seg3.empty:
            sections_drawn = True
            c.setFont("Helvetica-Bold", 11)
            y-= 20
            c.setFillColorRGB(*primary_color)
            c.drawString(table_margin, y, "Net ROAS <= 0.8")
            y -= 20
            y = draw_campaign_rows(seg3, y)
            # Section 3 Total Row (sum/average per new schema)
            seg3_total_spend = float(seg3.get('spend', 0).sum()) if 'spend' in seg3.columns else 0.0
            if 'shopify_revenue' in seg3.columns:
                seg3_total_sales = float(seg3['shopify_revenue'].sum())
            elif 'sales' in seg3.columns:
                seg3_total_sales = float(seg3['sales'].sum())
            else:
                seg3_total_sales = 0.0
            seg3_total_cogs = float(seg3.get('cogs', 0).sum()) if 'cogs' in seg3.columns else 0.0
            seg3_total_net_profit = seg3_total_sales - seg3_total_cogs - seg3_total_spend
            seg3_clicks_tot = float(seg3.get('clicks', 0).sum()) if 'clicks' in seg3.columns else 0.0
            seg3_impr_tot = float(seg3.get('impressions', 0).sum()) if 'impressions' in seg3.columns else 0.0
            seg3_avg_ctr = (seg3_clicks_tot / seg3_impr_tot * 100.0) if seg3_impr_tot > 0 else (float(seg3.get('ctr', 0).mean()) if 'ctr' in seg3.columns else 0.0)
            seg3_avg_bounce = float(seg3.get('bounce_rate', 0).mean()) if 'bounce_rate' in seg3.columns else 0.0
            seg3_avg_checkout = 0.0
            seg3_avg_gross_roas = (seg3_total_sales / seg3_total_spend) if seg3_total_spend > 0 else (float(seg3.get('gross_roas', 0).mean()) if 'gross_roas' in seg3.columns else float(seg3.get('roas', 0).mean()) if 'roas' in seg3.columns else 0.0)
            seg3_avg_net_roas = ((seg3_total_sales - seg3_total_cogs) / seg3_total_spend) if seg3_total_spend > 0 and 'cogs' in seg3.columns else (float(seg3.get('net_roas', 0).mean()) if 'net_roas' in seg3.columns else 0.0)
            seg3_avg_be_roas = ((seg3_total_cogs + seg3_total_spend) / seg3_total_spend) if seg3_total_spend > 0 and 'cogs' in seg3.columns else (float(seg3.get('be_roas', 0).mean()) if 'be_roas' in seg3.columns else float(seg3.get('breakeven_roas', 0).mean()) if 'breakeven_roas' in seg3.columns else 0.0)
            # Calculate average conversion rate from individual campaigns in section 3
            seg3_avg_cr = float(seg3.get('conversion_rate', 0).mean()) if 'conversion_rate' in seg3.columns else 0.0
            logger.info(f"Section 3 CR Debug: Individual CRs={seg3.get('conversion_rate', []).tolist()}, Average CR={seg3_avg_cr:.2f}%")
            c.setFillColorRGB(*light_gray)
            c.rect(table_margin, y - 6, table_width, 22, fill=True, stroke=False)
            c.setFillColorRGB(*primary_color)
            c.setFont("Helvetica-Bold", 9)
            c.drawString(x_positions[0] + 8, y, "Section Total")
            
            # Calculate percentages for spend and sales
            spend_percentage = round((seg3_total_spend / total_spend * 100), 1) if total_spend > 0 else 0.0
            sales_percentage = round((seg3_total_sales / total_sales * 100), 1) if total_sales > 0 else 0.0
            
            # First line: amounts only
            rtot(1, f"Rs {seg3_total_spend:.2f}")
            rtot(2, f"Rs {seg3_total_sales:.2f}")
            rtot(3, f"Rs {seg3_total_net_profit:.2f}")
            rtot(4, f"{seg3_avg_ctr:.2f}%")
            rtot(5, f"{seg3_avg_bounce:.2f}%")
            rtot(6, f"{seg3_avg_cr:.2f}%")
            rtot(7, f"{seg3_avg_gross_roas:.2f}")
            rtot(8, f"{seg3_avg_net_roas:.2f}")
            for i in range(len(col_widths)):
                c.rect(x_positions[i], y - 6, col_widths[i], 22, stroke=True, fill=False)
            # Second line: percentages under Spend and Revenue
            try:
                c.setFont("Helvetica", 7)
                c.setFillColorRGB(0, 0, 0)
                c.drawRightString(x_positions[1] + col_widths[1] - 8, y - 14, f"({spend_percentage}%)")
                c.drawRightString(x_positions[2] + col_widths[2] - 8, y - 14, f"({sales_percentage}%)")
            except Exception:
                pass
            y -= 24   
        
        # --- PMF Campaigns Section (Moved to bottom) ---
        if not pmf_campaigns.empty:
            y -= 35
            c.setFont("Helvetica-Bold", 12)
            c.setFillColorRGB(*primary_color)
            c.drawString(margin, y, "PMF Campaigns Performance")
            y -= 25
            
            # Draw header for PMF campaigns with enhanced styling
            c.setFillColorRGB(*light_gray)
            c.rect(table_margin, y - 6, table_width, 22, fill=True, stroke=False)
            c.setFillColorRGB(*primary_color)
            c.setFont("Helvetica-Bold", 9)
            for i, header in enumerate(headers):
                c.drawString(x_positions[i] + 8, y, header)
            y -= 22
            
            def draw_pmf_campaign_rows(df, y):
                for _, row in df.iterrows():
                    if y < margin + 30:
                        c.showPage()
                        y = height - margin
                        c.setFillColorRGB(*light_gray)
                        c.rect(table_margin, y - 6, table_width, 26, fill=True, stroke=False)
                        c.setFillColorRGB(*primary_color)
                        c.setFont("Helvetica-Bold", 12)
                        for i, header in enumerate(headers):
                            c.drawString(x_positions[i] + 8, y, header)
                        y -= 26
                    row_height = 32  # Increased height to accommodate wrapped campaign names
                    
                    # Gradient yellow intensity for CTR cell based on value (0-100)
                    try:
                        ctr_val = float(row.get('ctr', 0) or 0)
                        if ctr_val > 0:
                            t = max(0.0, min(100.0, ctr_val)) / 100.0
                            start_r, start_g, start_b = (1.00, 1.00, 0.92)
                            end_r, end_g, end_b = (1.00, 0.90, 0.10)
                            r = start_r + (end_r - start_r) * t
                            g = start_g + (end_g - start_g) * t
                            b = start_b + (end_b - start_b) * t
                            c.setFillColorRGB(r, g, b)
                            c.rect(x_positions[4], y - row_height + 14, col_widths[4], row_height, fill=True, stroke=False)
                            c.setFillColorRGB(0, 0, 0)
                    except Exception:
                        pass

                    # Gradient blue intensity for Bounce Rate cell based on value (0-100)
                    try:
                        bounce_val = float(row.get('bounce_rate', 0) or 0)
                        if bounce_val > 0:
                            # Normalize to [0,1]
                            t = max(0.0, min(100.0, bounce_val)) / 100.0
                            # From very light blue to professional blue
                            start_r, start_g, start_b = (0.95, 0.97, 1.0)
                            end_r, end_g, end_b = (0.10, 0.40, 0.70)
                            r = start_r + (end_r - start_r) * t
                            g = start_g + (end_g - start_g) * t
                            b = start_b + (end_b - start_b) * t
                            c.setFillColorRGB(r, g, b)
                            c.rect(x_positions[5], y - row_height + 14, col_widths[5], row_height, fill=True, stroke=False)
                            c.setFillColorRGB(0, 0, 0)
                    except Exception:
                        pass

                    # Red background for Net ROAS < 1 in PMF campaigns
                    try:
                        net_roas_val = float(row.get('net_roas', 0) or 0)
                        if net_roas_val < 1 and net_roas_val > 0:  # Only highlight if > 0 to avoid highlighting 0 values
                            c.setFillColorRGB(1.0, 0.78, 0.81)  # Light red background
                            c.rect(x_positions[8], y - row_height + 14, col_widths[8], row_height, fill=True, stroke=False)
                            c.setFillColorRGB(0, 0, 0)
                    except Exception:
                        pass
                    
                    for i in range(len(col_widths)):
                        c.rect(x_positions[i], y - row_height + 14, col_widths[i], row_height, stroke=True, fill=False)
                    draw_wrapped_campaign_name(c, row['campaign_name'], x_positions[0], y, col_widths[0], font_size=9)
                    c.setFont("Helvetica", 8)
                    def pr(ix, text):
                        c.drawRightString(x_positions[ix] + col_widths[ix] - 8, y, text)
                    def safe_get(row, key, default=0):
                        val = row.get(key, default)
                        if hasattr(val, 'iloc'):
                            return val.iloc[0] if len(val) > 0 else default
                        return val
                    
                    # Match regular campaigns column order: Campaign Name, Spend, Revenue, Net Profit, CTR, Bounce, CR, GR, NR
                    pr(1, f"Rs {safe_get(row, 'spend'):.2f}")
                    pr(2, f"Rs {safe_get(row, 'sales', safe_get(row, 'shopify_revenue')):.2f}")
                    pr(3, f"Rs {safe_get(row, 'net_profit'):.2f}")
                    pr(4, f"{safe_get(row, 'ctr'):.2f}%")
                    pr(5, f"{safe_get(row, 'bounce_rate'):.2f}%")
                    pr(6, f"{safe_get(row, 'conversion_rate'):.2f}%")
                    pr(7, f"{safe_get(row, 'roas', safe_get(row, 'gross_roas')):.2f}")
                    pr(8, f"{safe_get(row, 'net_roas'):.2f}")
                    y -= row_height
                return y
            
            # Draw PMF campaigns
            y = draw_pmf_campaign_rows(pmf_campaigns, y)
            
            # PMF Total Row with enhanced styling (matching regular campaign sections)
            pmf_total_spend = float(pmf_campaigns.get('spend', 0).sum()) if 'spend' in pmf_campaigns.columns else 0.0
            if 'shopify_revenue' in pmf_campaigns.columns:
                pmf_total_sales = float(pmf_campaigns['shopify_revenue'].sum())
            elif 'sales' in pmf_campaigns.columns:
                pmf_total_sales = float(pmf_campaigns['sales'].sum())
            else:
                pmf_total_sales = 0.0
            pmf_total_cogs = float(pmf_campaigns.get('cogs', 0).sum()) if 'cogs' in pmf_campaigns.columns else 0.0
            pmf_total_net_profit = pmf_total_sales - pmf_total_cogs - pmf_total_spend
            
            # CTR/CR from totals for consistency
            pmf_clicks_tot = float(pmf_campaigns.get('clicks', 0).sum()) if 'clicks' in pmf_campaigns.columns else 0.0
            pmf_impr_tot = float(pmf_campaigns.get('impressions', 0).sum()) if 'impressions' in pmf_campaigns.columns else 0.0
            pmf_avg_ctr = (pmf_clicks_tot / pmf_impr_tot * 100.0) if pmf_impr_tot > 0 else (float(pmf_campaigns.get('ctr', 0).mean()) if 'ctr' in pmf_campaigns.columns else 0.0)
            pmf_avg_bounce = float(pmf_campaigns.get('bounce_rate', 0).mean()) if 'bounce_rate' in pmf_campaigns.columns else 0.0
            
            # ROAS: prefer recompute from sums when possible
            pmf_avg_gross_roas = (pmf_total_sales / pmf_total_spend) if pmf_total_spend > 0 else (float(pmf_campaigns.get('gross_roas', 0).mean()) if 'gross_roas' in pmf_campaigns.columns else float(pmf_campaigns.get('roas', 0).mean()) if 'roas' in pmf_campaigns.columns else 0.0)
            pmf_avg_net_roas = ((pmf_total_sales - pmf_total_cogs) / pmf_total_spend) if pmf_total_spend > 0 and 'cogs' in pmf_campaigns.columns else (float(pmf_campaigns.get('net_roas', 0).mean()) if 'net_roas' in pmf_campaigns.columns else 0.0)
            
            # Calculate average conversion rate from individual campaigns in PMF section
            pmf_avg_cr = float(pmf_campaigns.get('conversion_rate', 0).mean()) if 'conversion_rate' in pmf_campaigns.columns else 0.0
            logger.info(f"PMF CR Debug: Individual CRs={pmf_campaigns.get('conversion_rate', []).tolist()}, Average CR={pmf_avg_cr:.2f}%")
            
            # Add debugging for all PMF metrics
            logger.info(f"PMF Total Debug - Spend: {pmf_total_spend}, Sales: {pmf_total_sales}, COGS: {pmf_total_cogs}, Net Profit: {pmf_total_net_profit}")
            logger.info(f"PMF Total Debug - CTR: {pmf_avg_ctr:.2f}%, Bounce: {pmf_avg_bounce:.2f}%, CR: {pmf_avg_cr:.2f}%")
            logger.info(f"PMF Total Debug - Gross ROAS: {pmf_avg_gross_roas:.2f}, Net ROAS: {pmf_avg_net_roas:.2f}")
            
            # Minimal PMF total row styling
            c.setFillColorRGB(*light_gray)
            c.rect(table_margin, y - 6, table_width, 22, fill=True, stroke=False)
            c.setFillColorRGB(*primary_color)
            c.setFont("Helvetica-Bold", 9)
            c.drawString(x_positions[0] + 8, y, "PMF Total")
            
            # Calculate percentages for spend and sales
            spend_percentage = round((pmf_total_spend / total_spend * 100), 1) if total_spend > 0 else 0.0
            sales_percentage = round((pmf_total_sales / total_sales * 100), 1) if total_sales > 0 else 0.0
            
            def rtot(ix, text):
                c.drawRightString(x_positions[ix] + col_widths[ix] - 8, y, text)
            # First line: amounts only - Match regular campaigns column order
            rtot(1, f"Rs {pmf_total_spend:.2f}")
            rtot(2, f"Rs {pmf_total_sales:.2f}")
            rtot(3, f"Rs {pmf_total_net_profit:.2f}")
            rtot(4, f"{pmf_avg_ctr:.2f}%")
            rtot(5, f"{pmf_avg_bounce:.2f}%")
            rtot(6, f"{pmf_avg_cr:.2f}%")
            rtot(7, f"{pmf_avg_gross_roas:.2f}")
            rtot(8, f"{pmf_avg_net_roas:.2f}")
            for i in range(len(col_widths)):
                c.rect(x_positions[i], y - 6, col_widths[i], 22, stroke=True, fill=False)
            # Second line: percentages under Spend and Revenue
            try:
                c.setFont("Helvetica", 7)
                c.setFillColorRGB(0, 0, 0)
                c.drawRightString(x_positions[1] + col_widths[1] - 8, y - 14, f"({spend_percentage}%)")
                c.drawRightString(x_positions[2] + col_widths[2] - 8, y - 14, f"({sales_percentage}%)")
            except Exception:
                pass
            y -= 24
        
        # Only draw overall totals if there were sections with data (minimal styling)
        if sections_drawn or not pmf_campaigns.empty:
            # Add 20px space above overall total row
            y -= 20
            
            # Minimal total row with uniform light background (no gradients)
            c.setFillColorRGB(*light_gray)
            c.rect(table_margin, y - 6, table_width, 22, fill=True, stroke=False)
            # Ensure each cell has uniform background (override any potential gradients)
            for i in range(len(col_widths)):
                c.setFillColorRGB(*light_gray)
                c.rect(x_positions[i], y - 6, col_widths[i], 22, fill=True, stroke=False)
            c.setFillColorRGB(*primary_color)
            c.setFont("Helvetica-Bold", 10)
            c.drawString(x_positions[0] + 8, y, "Overall Total")
            def orr(ix, text):
                c.drawRightString(x_positions[ix] + col_widths[ix] - 8, y, text)
            # Prefer authoritative grand totals from dailyrollup
            try:
                # Use provided timeframe parameters if available, otherwise use timeframe_config
                if timeframe_start is not None and timeframe_end is not None:
                    # Convert datetime to date strings for the function
                    start_date_str = timeframe_start.strftime('%Y-%m-%d') if hasattr(timeframe_start, 'strftime') else str(timeframe_start)
                    end_date_str = timeframe_end.strftime('%Y-%m-%d') if hasattr(timeframe_end, 'strftime') else str(timeframe_end)
                    gt = get_campaign_grand_total_for_pdf(start_date=start_date_str, end_date=end_date_str) or {}
                else:
                    # Use global timeframe from timeframe_config
                    gt = get_campaign_grand_total_for_pdf() or {}
                logger.info("Loaded dailyrollup grand total payload")
                # Recalculate rates explicitly from provided totals for full consistency
                gt_clicks = float(gt.get('clicks', 0) or 0)
                gt_impr = float(gt.get('impressions', 0) or 0)
                gt_lpv = float(gt.get('lpv', 0) or 0)
                gt_ic = float(gt.get('initiate_checkout', 0) or 0)
                gt_ctr = (gt_clicks / gt_impr * 100.0) if gt_impr > 0 else 0.0
                gt_bounce = ((gt_clicks - gt_lpv) / gt_clicks * 100.0) if gt_clicks > 0 else 0.0
                gt_checkout = (gt_ic / gt_lpv * 100.0) if gt_lpv > 0 else 0.0
                gt_conv = (float(gt.get('shopify_orders', 0) or 0) / gt_clicks * 100.0) if gt_clicks > 0 else 0.0

                orr(1, f"Rs {float(gt.get('spend', total_spend)):.2f}")
                orr(2, f"Rs {float(gt.get('shopify_revenue', total_sales)):.2f}")
                orr(3, f"Rs {float(gt.get('net_profit', total_net_profit) or 0):.2f}")
                orr(4, f"{gt_ctr:.2f}%")
                orr(5, f"{gt_bounce:.2f}%")
                orr(6, f"{gt_conv:.2f}%")
                orr(7, f"{float(gt.get('gross_roas', total_gross_roas)):.2f}")
                orr(8, f"{float(gt.get('net_roas', total_net_roas)):.2f}")
            except Exception:
                # Fallback: use locally computed totals
                orr(1, f"Rs {total_spend:.2f}")
                orr(2, f"Rs {total_sales:.2f}")
                orr(3, f"Rs {float(total_net_profit or 0):.2f}")
                orr(4, f"{total_ctr:.2f}%")
                orr(5, f"{total_bounce:.2f}%")
                orr(6, f"{total_cr:.2f}%")
                orr(7, f"{total_gross_roas:.2f}")
                orr(8, f"{total_net_roas:.2f}")
            c.setStrokeColorRGB(*border_color)
            for i in range(len(col_widths)):
                c.rect(x_positions[i], y - 6, col_widths[i], 22, stroke=True, fill=False)
            # Add a note at the bottom of the table with enhanced styling
            y -= 30
            c.setFont("Helvetica-Oblique", 9)
            c.setFillColorRGB(0.5, 0.1, 0.1)
            c.drawString(table_margin, y, "Note: Campaign totals are computed from the displayed dailyrollup campaign data.")
            c.setFillColorRGB(0, 0, 0)
            y -= 25
        else:
            # If no campaigns found, add a note with enhanced styling
            c.setFont("Helvetica", 11)
            c.setFillColorRGB(*secondary_color)
            c.drawString(table_margin, y, "No campaigns found")
            y -= 25
        
        # Footer with enhanced styling
        y -= 30
        c.setFont("Helvetica", 9)
        c.setFillColorRGB(*secondary_color)
        c.drawString(margin, y, "Generated by Marketing Analytics System")
        
        c.save()
        logger.info(f"PDF report saved to: {report_name}")
        return report_name
    except Exception as e:
        logger.error(f"Failed to generate PDF report: {str(e)}")
        raise

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
    email_body = f"""
    <html>
    <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
        <p>Dear Team,</p>
        <p>Please find below the Daily Marketing Performance Report for {today_str}.</p>
        <h2>Montly Performance</h2>
    """
    
    daily_plot_cid = None
    historical_plot_cid = None
    shopify_profit_plot_cid = None
    hourly_sales_plot_cid = None  # NEW: For hourly sales plot
    sales_by_state_pie_cid = None

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
            elif 'historical_insights' in base_filename:
                descriptive_name = f"Historical Trends Plot - {today_str}.png"
                historical_plot_cid = f"historical_plot_{today_str}"
                content_id = historical_plot_cid
                is_inline = True
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

    # Conditionally add plot images to email_body after processing all attachments
    if daily_plot_cid:
        email_body += f"""
        <p><img src=\"cid:{daily_plot_cid}\" alt=\"Daily Performance Plot\" style=\"max-width: 600px; width: 100%; height: auto;\"></p>
        """
    else:
        email_body += "<p><em>Daily Performance Plot unavailable. No data available for this period.</em></p>"

    if shopify_profit_plot_cid:
        email_body += f"""
        <p><img src=\"cid:{shopify_profit_plot_cid}\" alt=\"Daily Shopify Net Profit Plot\" style=\"max-width: 600px; width: 100%; height: auto;\"></p>
        """
    else:
        email_body += "<p><em>Daily Shopify Net Profit Plot unavailable. No data available for this period.</em></p>"

    # NEW: Add Hourly Sales Plot for Last 7 Days
    if hourly_sales_plot_cid:
        email_body += f"""
        <p><img src=\"cid:{hourly_sales_plot_cid}\" alt=\"Hourly Sales (Last 7 Days) Plot\" style=\"max-width: 600px; width: 100%; height: auto;\"></p>
        """
    else:
        email_body += "<p><em>Hourly Sales (Last 7 Days) Plot unavailable. No data available for this period.</em></p>"

    if sales_by_state_pie_cid:
        email_body += f"""
        <p><img src=\"cid:{sales_by_state_pie_cid}\" alt=\"Sales by state\" style=\"max-width: 600px; width: 100%; height: auto;\"></p>
        """

    email_body += f"""
        <em>Generated automatically by the Marketing Insights System</em></p>
    </body>
    </html>
    """
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
        for file_path in file_paths_to_attach:
            if not file_path:
                continue  # Skip None or empty paths
            try:
                os.remove(file_path)
                logger.info(f"Deleted file: {file_path}")
            except Exception as e:
                logger.warning(f"Failed to delete file {file_path}: {e}")
        
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
    # Calculate campaign summary
    campaign_summary = df.groupby('campaign_name').agg({
        'spend': 'sum',
        'sales': 'sum',
        'impressions': 'sum',
        'clicks': 'sum',
        'purchases': 'sum',
        'breakeven_roas': 'mean'
    }).reset_index()
    
    # Calculate margin and ROAS metrics
    df['total_margin'] = df['net_margin'] * df['purchases']
    campaign_summary['total_margin'] = df.groupby('campaign_name')['total_margin'].sum().values
    
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
        # Use our standardized timeframe configuration
        timeframe = get_timeframe_config(days_range=1, use_fixed_dates=True)
        today, timestamp_str = timeframe['today'], timeframe['timestamp_str']
        from datetime import datetime
        if isinstance(today, str):
            today = datetime.strptime(today, "%Y-%m-%d")
        logger.info(f"Starting export and report generation at {timestamp_str}")
        os.makedirs(REPORT_DIR, exist_ok=True)

        # Generate dailyrollup Excel for the timeframe and use its campaign data in PDF
        try:
            # Use global timeframe from timeframe_config instead of explicit single date
            xlsx_path = run_dailyrollup(out_dir=REPORT_DIR)
            logger.info(f"Dailyrollup Excel generated: {xlsx_path}")
        except Exception as e:
            logger.warning(f"Dailyrollup Excel generation failed: {e}")

        # Fetch meta data and process it to get metrics (for campaign data)
        data = fetch_meta_data(today)
        metrics = process_data(data, today, timestamp_str)

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

        # --- Add Hourly Sales Plot for Last 7 Days ---
        from datetime import datetime
        today_str = today.strftime('%Y-%m-%d') if isinstance(today, datetime) else str(today)
        hourly_sales_plot_path = os.path.join(REPORT_DIR, f"hourly_sales_last_7_days_{today_str}.png")
        if os.path.exists(hourly_sales_plot_path):
            plot_files = plot_files or []
            plot_files.append(hourly_sales_plot_path)
            logger.info(f"Added hourly sales plot to attachments: {hourly_sales_plot_path}")
        else:
            logger.warning(f"Hourly sales plot not found: {hourly_sales_plot_path}")

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
