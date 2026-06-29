import requests
import json
import seaborn as sns
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta
import os
import sys
import matplotlib.dates as mdates
import pytz
from sqlalchemy import create_engine
import logging
import matplotlib
from api_data_fetcher import (
    fetch_db_sales,
    fetch_net_profit_from_db,
    fetch_net_profit_single_day,
    fetch_shopify_sales_by_state,
    get_api_headers,
    set_api_bearer_token,
)
from revenue_gst import apply_net_revenue_column
from global_config import get_global_config, get_facebook_ads_config
from channel_performance import plot_channel_performance_daily
matplotlib.use('Agg')
matplotlib.rcParams['font.family'] = 'DejaVu Sans'

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
        _log_file = os.path.join(_log_dir, 'plots.log')
        handlers = [
            logging.FileHandler(_log_file),
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

# Database configuration from global config
DB_CONFIG = {
    'host': get_global_config('DB_HOST', '72.61.228.168'),
    'port': int(get_global_config('DB_PORT', 5432)),
    'database': get_global_config('DB_NAME', 'seleric_db'),
    'user': get_global_config('DB_USER', 'admin_seleric'),
    'password': get_global_config('DB_PASSWORD', 'SelericDB246'),
    'sslmode': 'require'
}

# --- Configuration ---
DAYS_TO_FETCH = 30
MONTHS_FOR_HISTORICAL = 1
# Use /tmp/reports in Azure Functions, otherwise use get_report_dir or temp directory
if os.environ.get('WEBSITE_INSTANCE_ID'):  # Azure Functions environment
    REPORT_DIR = '/tmp/reports'
else:
    try:
        from global_config import get_report_dir
        REPORT_DIR = get_report_dir()
    except (ImportError, PermissionError, OSError):
        # Fallback to temp directory
        import tempfile
        REPORT_DIR = os.path.join(tempfile.gettempdir(), 'reports')
os.makedirs(REPORT_DIR, exist_ok=True)

# --- Timezone Configuration ---
INDIAN_TZ = pytz.timezone('Asia/Kolkata')  # Indian Standard Time (UTC+5:30)

# --- Function to Fetch Data from Facebook API Directly ---
def fetch_daily_insights_from_api(days, access_token, account_id, api_base_url="https://graph.facebook.com/v19.0"):
    """
    Fetch daily insights data for the last N days directly from Facebook Marketing API.
    """
    import pytz
    INDIAN_TZ = pytz.timezone('Asia/Kolkata')
    from datetime import datetime, timedelta
    from timeframe_config import get_timeframe_config
    
    # Use timeframe_config for consistent date handling
    tf = get_timeframe_config()
    end_date = tf['end_date']
    start_date = end_date - timedelta(days=days - 1)
    since_date = start_date.strftime('%Y-%m-%d')
    until_date = end_date.strftime('%Y-%m-%d')
    fields = ["spend", "action_values"]
    params = {
        "fields": ",".join(fields),
        "time_range": f'{{"since":"{since_date}","until":"{until_date}"}}',
        "time_increment": 1,
        "limit": 10000,
        "access_token": access_token
    }
    api_url = f"{api_base_url}/act_{account_id}/insights"
    try:
        response = requests.get(api_url, params=params)
        response.raise_for_status()
        data = response.json()
        return data.get('data', []), None
    except Exception as e:
        return None, str(e)

def fetch_historical_hourly_insights_from_api(months, access_token, account_id, api_base_url="https://graph.facebook.com/v19.0"):
    """
    Fetch historical hourly insights data for the last N months directly from Facebook Marketing API.
    """
    import pytz
    INDIAN_TZ = pytz.timezone('Asia/Kolkata')
    from datetime import datetime
    import calendar
    now = datetime.now(INDIAN_TZ)
    current_weekday = now.weekday()
    current_hour = now.hour
    # Calculate since_date as N months ago (approximate as N*30 days)
    first_day_of_current_month = now.replace(day=1)
    year = first_day_of_current_month.year - (months // 12)
    month = first_day_of_current_month.month - (months % 12)
    if month <= 0:
        month += 12
        year -= 1
    since_date_dt = first_day_of_current_month.replace(year=year, month=month)
    since_date = since_date_dt.strftime('%Y-%m-%d')
    until_date = now.strftime('%Y-%m-%d')
    fields = ["spend", "action_values"]
    params = {
        "fields": ",".join(fields),
        "time_range": f'{{"since":"{since_date}","until":"{until_date}"}}',
        "breakdowns": "hourly_stats_aggregated_by_advertiser_time_zone",
        "time_increment": 1,
        "limit": 200,
        "access_token": access_token
    }
    api_url = f"{api_base_url}/act_{account_id}/insights"
    all_hourly_data = []
    next_page_url = api_url
    try:
        page_count = 0
        while next_page_url:
            page_count += 1
            if page_count == 1:
                response = requests.get(next_page_url, params=params)
            else:
                response = requests.get(next_page_url)
            response.raise_for_status()
            data = response.json()
            current_page_data = data.get('data', [])
            all_hourly_data.extend(current_page_data)
            if 'paging' in data and 'next' in data['paging']:
                next_page_url = data['paging']['next']
            else:
                next_page_url = None
        # Filter data for current day of week and hour
        filtered_data = []
        for record in all_hourly_data:
            record_date_str = record.get('date_start')
            record_hour_range_str = record.get('hourly_stats_aggregated_by_advertiser_time_zone')
            if record_date_str and record_hour_range_str is not None:
                try:
                    record_date = datetime.strptime(record_date_str, '%Y-%m-%d')
                    record_date = INDIAN_TZ.localize(record_date) if record_date.tzinfo is None else record_date.astimezone(INDIAN_TZ)
                    record_weekday = record_date.weekday()
                    record_hour = int(record_hour_range_str.split(':')[0])
                    if record_weekday == current_weekday and record_hour == current_hour:
                        filtered_data.append(record)
                except Exception:
                    continue
        return filtered_data, calendar.day_name[current_weekday], current_hour, None
    except Exception as e:
        return None, None, None, str(e)

# --- Function to Fetch ROAS Data from API ---
def fetch_roas_data_from_api(start_date, end_date):
    """
    Fetch ROAS data from the API for the specified date range.
    Returns the API response data or None if failed.
    """
    import requests
    from datetime import datetime, timedelta
    
    # Calculate date range for last 30 days if not provided
    if not start_date or not end_date:
        end_date = datetime.now()
        start_date = end_date - timedelta(days=29)
        start_date = start_date.strftime('%Y-%m-%d')
        end_date = end_date.strftime('%Y-%m-%d')
    
    api_url = f"https://node.seleric.cloud//api/roas_by_date"
    params = {
        'start_date': start_date,
        'end_date': end_date
    }
    
    try:
        print(f"Fetching ROAS data from API for date range: {start_date} to {end_date}")
        response = requests.get(api_url, params=params, headers=get_api_headers(), timeout=30)
        response.raise_for_status()
        data = response.json()
        
        if data.get('success'):
            print(f"Successfully fetched ROAS data for {len(data.get('roas_by_date', []))} days")
            return data
        else:
            print(f"API returned success=False: {data}")
            return None
            
    except requests.exceptions.RequestException as e:
        print(f"Error fetching ROAS data from API: {e}")
        return None
    except Exception as e:
        print(f"Unexpected error fetching ROAS data: {e}")
        return None

# --- Function to Plot Daily Spend, Purchase Value, and ROAS ---
def plot_daily_amounts(daily_insights_data, save_path=None):
    """
    Plots Daily Spend, Purchase Value, and Net ROAS using Seaborn with a secondary y-axis for ROAS.
    Uses API data for net ROAS, ad spend, and revenue data.
    Returns None if no valid data is available.
    """
    if not daily_insights_data:
        print("No daily insights data available to plot.")
        return None

    # Get date range from timeframe_config
    from timeframe_config import get_timeframe_config
    from datetime import timedelta
    tf = get_timeframe_config()
    end_date = tf['end_date']
    start_date = end_date - timedelta(days=29)  # 30 days total (end_date - 29 days = 30 days)
    start_date_str = start_date.strftime('%Y-%m-%d')
    end_date_str = end_date.strftime('%Y-%m-%d')
    
    # Fetch net profit data from DB to calculate net ROAS date-wise
    print("Fetching net profit data from DB to calculate net ROAS...")
    db_result = fetch_net_profit_from_db(n_days=30)
    db_df = db_result.get("dailyBreakdown", pd.DataFrame())

    # Create lookup dictionary keyed by date string
    api_data_lookup = {}

    if not db_df.empty:
        print(f"Loaded {len(db_df)} days of net profit data from DB")
        for _, row in db_df.iterrows():
            date_str = str(row['sale_date'])[:10]
            revenue  = float(row.get('revenue', 0) or 0)
            cogs     = float(row.get('cogs', 0) or 0)
            ad_spend = float(row.get('total_ad_spend', 0) or 0)
            net_roas = round((revenue - cogs) / ad_spend, 2) if ad_spend > 0 else 0.0
            api_data_lookup[date_str] = {
                'revenue':    revenue,
                'ad_spend':   ad_spend,
                'cogs':       cogs,
                'net_roas':   net_roas,
                'net_profit': float(row.get('net_profit', 0) or 0),
            }
        print(f"Processed {len(api_data_lookup)} days of data with calculated net ROAS")
        sample = next(iter(api_data_lookup.values())) if api_data_lookup else {}
        if sample:
            print(f"Sample DB record - revenue: {sample['revenue']}, cogs: {sample['cogs']}, ad_spend: {sample['ad_spend']}, net_roas: {sample['net_roas']}")
    else:
        logger.warning("Failed to fetch net profit data from DB. Falling back to gross ROAS fallback.")

    # Process data for plotting
    dates = []
    spend_values = []
    purchase_values = []
    roas_values = []

    print("Processing API ROAS data for plotting...")
    
    # Create a complete date range for the last 30 days
    from datetime import datetime, timedelta
    end_date = datetime.now()
    start_date = end_date - timedelta(days=29)  # 30 days total
    
    # Generate all dates in the range
    all_dates = []
    current_date = start_date
    while current_date <= end_date:
        all_dates.append(current_date.strftime('%Y-%m-%d'))
        current_date += timedelta(days=1)
    
    print(f"Generated {len(all_dates)} dates for the range: {all_dates[0]} to {all_dates[-1]}")
    
    # Process each date in the range
    for date_str in all_dates:
        try:
            date = pd.to_datetime(date_str)
            dates.append(date)
        except ValueError:
            print(f"Warning: Skipping invalid date format: {date_str}")
            continue

        # Get data from API lookup (will be empty dict if no data for this date)
        api_record = api_data_lookup.get(date_str, {})
        
        # Get ad spend from API data (0 if no data)
        spend = api_record.get('ad_spend', 0)
        spend_values.append(spend)

        # Get revenue from API data (0 if no data)
        purchase_value = api_record.get('revenue', 0)
        purchase_values.append(purchase_value)

        # Get net ROAS from API data (calculated from revenue, cogs, and ad_spend)
        # IMPORTANT: Using net_roas (accounts for COGS and other costs), NOT gross_roas
        net_roas = api_record.get('net_roas', 0)
        # If net_roas is 0 or missing, try to calculate it from available data
        if net_roas == 0 and api_record.get('revenue', 0) > 0 and api_record.get('ad_spend', 0) > 0:
            revenue = api_record.get('revenue', 0)
            cogs = api_record.get('cogs', 0)
            ad_spend = api_record.get('ad_spend', 0)
            if ad_spend > 0:
                net_roas = (revenue - cogs) / ad_spend
        roas_values.append(round(net_roas, 2) if net_roas else 0)

    if not dates or (not any(spend_values) and not any(purchase_values) and not any(roas_values)):
        print("No valid data points for daily insights after processing.")
        return None

    print("Data processing complete for daily insights.")

    # Create DataFrame for Seaborn
    df = pd.DataFrame({
        'Date': dates,
        'Spend': spend_values,
        'Purchase Value': purchase_values,
        'ROAS': roas_values
    })
    df['Date'] = pd.to_datetime(df['Date'])

    # Set Seaborn style
    sns.set_style("whitegrid")

    # Create figure and axes with secondary y-axis
    fig, ax1 = plt.subplots(figsize=(10, 6))
    ax2 = ax1.twinx()
    
    # Move Amount axis to the right side
    ax1.yaxis.set_label_position('right')
    ax1.yaxis.tick_right()

    # Plot bars for Spend and Purchase Value
    bar_width = 0.4
    x = range(len(dates))
    ax1.bar([i - bar_width/2 for i in x], df['Spend'], width=bar_width/2, label='Ad Spend', color='#1f77b4')
    ax1.bar([i + bar_width/2 for i in x], df['Purchase Value'], width=bar_width/2, label='Revenue', color='#2ca02c')

    # Plot line for Net ROAS with label for legend on secondary axis
    line_plot = sns.lineplot(x=x, y='ROAS', data=df, ax=ax2, color='#d62728', marker='o', linewidth=1.5, markersize=8, label='Net ROAS')

    # Add value labels on ROAS data points - positioned on the actual points
    for i, (x_pos, roas_val) in enumerate(zip(x, df['ROAS'])):
        if roas_val > 0:  # Only show labels for non-zero ROAS values
            ax2.annotate(f'{roas_val:.2f}', 
                        (x_pos, roas_val), 
                        textcoords="data", 
                        ha='center', 
                        va='center',
                        fontsize=9, 
                        fontweight='bold',
                        color='white',
                        bbox=dict(boxstyle="round,pad=0.2", facecolor='#d62728', edgecolor='#d62728', alpha=0.9))

    # Customize axes
    ax1.set_xlabel('Date')
    ax1.set_ylabel('Amount (₹)', color='#1f77b4')
    ax1.set_xticks(x)
    ax1.set_xticklabels([d.strftime('%Y-%m-%d') for d in df['Date']], rotation=90)
    ax1.tick_params(axis='y', colors='#1f77b4')
    
    # Adjust x-axis to align dates with the center of the bar groups
    ax1.set_xlim(-0.5, len(x) - 0.5)
    
    # Hide ROAS axis but keep the data visible
    ax2.set_ylabel('')  # Remove y-axis label
    ax2.set_yticks([])  # Hide y-axis ticks
    ax2.spines['right'].set_visible(False)  # Hide right spine
    
    # Adjust ROAS axis scale to make the line more prominent
    roas_max = df['ROAS'].max()
    roas_min = df['ROAS'].min()
    roas_range = roas_max - roas_min
    if roas_range > 0:
        # Set ROAS axis to start at 0 and show more of the chart area
        ax2.set_ylim(0, roas_max + roas_range * 0.3)
    
    # Ensure ROAS line is drawn on top of bars by setting zorder
    ax2.set_zorder(ax1.get_zorder() + 1)
    ax2.patch.set_visible(False)  # Make ax2 transparent so bars show through

    # Add legends
    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc='upper center', bbox_to_anchor=(0.5, -0.15), ncol=3)

    # Set title
    plt.title(f'Daily Ad Spend, Revenue, and Net ROAS for Last {len(dates)} Days', pad=20)

    # Adjust layout to prevent overlap and provide space for rotated labels
    plt.tight_layout()
    plt.subplots_adjust(bottom=0.2)  # Add extra space at bottom for rotated labels

    if save_path:
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        try:
            plt.savefig(save_path, dpi=200, bbox_inches='tight')
            print(f"Daily insights plot saved as: {save_path}")
            plt.close(fig)
            return save_path
        except Exception as e:
            print(f"Error saving daily insights plot as PNG: {e}")
            html_path = save_path.replace('.png', '.html')
            plt.savefig(html_path, format='html', bbox_inches='tight')
            print(f"Fallback: Daily insights plot saved as HTML: {html_path}")
            plt.close(fig)
            return html_path
    else:
        print("Showing daily insights plot...")
        plt.show()
        plt.close(fig)
        return None

def plot_daily_amounts_fallback(daily_insights_data, save_path=None):
    """
    Fallback function that uses the original implementation when API data is not available.
    """
    if not daily_insights_data:
        print("No daily insights data available to plot.")
        return None

    # Fetch database sales data for the same period as daily insights
    print("Fetching database sales data for purchase values...")
    db_sales_data = fetch_db_sales(n_days=len(daily_insights_data))
    
    # Create a lookup dictionary for database sales data
    db_sales_lookup = {}
    if not db_sales_data.empty:
        for _, row in db_sales_data.iterrows():
            date_key = pd.to_datetime(row['sale_date']).strftime('%Y-%m-%d')
            db_sales_lookup[date_key] = row['total_sales']
        print(f"Loaded {len(db_sales_lookup)} days of database sales data")
    else:
        print("Warning: No database sales data available")

    # Create a complete date range for the last 30 days
    from datetime import datetime, timedelta
    end_date = datetime.now()
    start_date = end_date - timedelta(days=29)  # 30 days total
    
    # Generate all dates in the range
    all_dates = []
    current_date = start_date
    while current_date <= end_date:
        all_dates.append(current_date.strftime('%Y-%m-%d'))
        current_date += timedelta(days=1)
    
    print(f"Generated {len(all_dates)} dates for the range: {all_dates[0]} to {all_dates[-1]}")
    
    # Create lookup for daily insights data
    daily_insights_lookup = {}
    for day_data in daily_insights_data:
        date_str = day_data.get('date_start')
        if date_str:
            daily_insights_lookup[date_str] = day_data
    
    dates = []
    spend_values = []
    purchase_values = []
    roas_values = []

    print("Processing daily insights data for plotting...")
    for date_str in all_dates:
        try:
            date = pd.to_datetime(date_str)
            dates.append(date)
        except ValueError:
            print(f"Warning: Skipping invalid date format: {date_str}")
            continue

        # Get data from daily insights lookup (will be None if no data for this date)
        day_data = daily_insights_lookup.get(date_str)
        
        # Get spend from API data (0 if no data)
        spend = 0
        if day_data:
            try:
                spend_str = day_data.get('spend')
                if spend_str is not None:
                    spend = float(spend_str)
            except (ValueError, TypeError):
                print(f"Warning: Invalid spend value for {date_str}: '{day_data.get('spend')}'. Using 0.")
                spend = 0
        spend_values.append(spend)

        # Get purchase value from database sales data instead of API
        purchase_value = db_sales_lookup.get(date_str, 0)
        purchase_values.append(purchase_value)

        # Handle 0 sales scenarios - ROAS is 0 when no sales, even if there's ad spend
        roas = 0 if purchase_value == 0 else (purchase_value / spend if spend > 0 else 0)
        roas_values.append(roas)

    if not dates or (not any(spend_values) and not any(purchase_values) and not any(roas_values)):
        print("No valid data points for daily insights after processing.")
        return None

    print("Data processing complete for daily insights.")

    # Create DataFrame for Seaborn
    df = pd.DataFrame({
        'Date': dates,
        'Spend': spend_values,
        'Purchase Value': purchase_values,
        'ROAS': roas_values
    })
    df['Date'] = pd.to_datetime(df['Date'])

    # Set Seaborn style
    sns.set_style("whitegrid")

    # Create figure and axes with secondary y-axis
    fig, ax1 = plt.subplots(figsize=(10, 6))
    ax2 = ax1.twinx()
    
    # Move Amount axis to the right side
    ax1.yaxis.set_label_position('right')
    ax1.yaxis.tick_right()

    # Plot bars for Spend and Purchase Value
    bar_width = 0.4
    x = range(len(dates))
    ax1.bar([i - bar_width/2 for i in x], df['Spend'], width=bar_width/2, label='Spend', color='#1f77b4')
    ax1.bar([i + bar_width/2 for i in x], df['Purchase Value'], width=bar_width/2, label='Revenue', color='#2ca02c')

    # Plot line for ROAS with label for legend on secondary axis
    line_plot = sns.lineplot(x=x, y='ROAS', data=df, ax=ax2, color='#d62728', marker='o', linewidth=1.5, markersize=8, label='Gross ROAS (fallback)')

    # Add value labels on ROAS data points - positioned on the actual points
    for i, (x_pos, roas_val) in enumerate(zip(x, df['ROAS'])):
        if roas_val > 0:  # Only show labels for non-zero ROAS values
            ax2.annotate(f'{roas_val:.2f}', 
                        (x_pos, roas_val), 
                        textcoords="data", 
                        ha='center', 
                        va='center',
                        fontsize=9, 
                        fontweight='bold',
                        color='white',
                        bbox=dict(boxstyle="round,pad=0.2", facecolor='#d62728', edgecolor='#d62728', alpha=0.9))

    # Customize axes
    ax1.set_xlabel('Date')
    ax1.set_ylabel('Amount', color='#1f77b4')
    ax1.set_xticks(x)
    ax1.set_xticklabels([d.strftime('%Y-%m-%d') for d in df['Date']], rotation=90)
    ax1.tick_params(axis='y', colors='#1f77b4')
    
    # Adjust x-axis to align dates with the center of the bar groups
    ax1.set_xlim(-0.5, len(x) - 0.5)
    
    # Hide ROAS axis but keep the data visible
    ax2.set_ylabel('')  # Remove y-axis label
    ax2.set_yticks([])  # Hide y-axis ticks
    ax2.spines['right'].set_visible(False)  # Hide right spine
    
    # Adjust ROAS axis scale to make the line more prominent
    roas_max = df['ROAS'].max()
    roas_min = df['ROAS'].min()
    roas_range = roas_max - roas_min
    if roas_range > 0:
        # Set ROAS axis to start at 0 and show more of the chart area
        ax2.set_ylim(0, roas_max + roas_range * 0.3)
    
    # Ensure ROAS line is drawn on top of bars by setting zorder
    ax2.set_zorder(ax1.get_zorder() + 1)
    ax2.patch.set_visible(False)  # Make ax2 transparent so bars show through

    # Add legends
    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc='upper center', bbox_to_anchor=(0.5, -0.15), ncol=3)

    # Set title indicating this is a fallback using gross ROAS
    plt.title(f'Daily Spend, Revenue, and Gross ROAS (fallback) for Last {len(dates)} Days', pad=20)

    # Log and annotate the chart so users can see that this is a fallback calculation
    logger.warning("Using fallback gross ROAS because net profit API returned no data or failed.")
    ax1.text(0.01, 0.95, 'Using Gross ROAS (fallback due to missing net profit API)', transform=ax1.transAxes,
             fontsize=9, color='orange', verticalalignment='top', bbox=dict(boxstyle='round', facecolor='yellow', alpha=0.3))

    # Adjust layout to prevent overlap and provide space for rotated labels
    plt.tight_layout()
    plt.subplots_adjust(bottom=0.2)  # Add extra space at bottom for rotated labels

    if save_path:
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        try:
            plt.savefig(save_path, dpi=200, bbox_inches='tight')
            print(f"Daily insights plot saved as: {save_path}")
            plt.close(fig)
            return save_path
        except Exception as e:
            print(f"Error saving daily insights plot as PNG: {e}")
            html_path = save_path.replace('.png', '.html')
            plt.savefig(html_path, format='html', bbox_inches='tight')
            print(f"Fallback: Daily insights plot saved as HTML: {html_path}")
            plt.close(fig)
            return html_path
    else:
        print("Showing daily insights plot...")
        plt.show()
        plt.close(fig)
        return None

# --- Function to Plot Historical Insights ---
def plot_historical_insights(historical_data, current_day_of_week, current_hour, save_path=None):
    """
    Plots Historical Spend and Web In-Store Purchase as a bar chart using Seaborn.
    Returns None if no valid data is available.
    """
    if not historical_data:
        print("No historical insights data available to plot.")
        return None

    historical_data.sort(key=lambda x: x.get('date_start', ''))
    hist_dates = []
    hist_spend_values = []
    hist_web_purchase_values = []

    print("Processing historical insights data for plotting...")
    for i, record in enumerate(historical_data):
        date_str = record.get('date_start')
        if not date_str:
            print(f"Warning: Skipping historical record {i} due to missing date_start: {record}")
            continue

        try:
            date = pd.to_datetime(date_str)
            hist_dates.append(date)
        except ValueError:
            print(f"Warning: Skipping historical record {i} due to invalid date format: {date_str}")
            continue

        spend = 0
        try:
            spend_str = record.get('spend')
            if spend_str is not None:
                spend = float(spend_str)
            hist_spend_values.append(spend)
        except (ValueError, TypeError):
            print(f"Warning: Invalid spend value for {date_str}: '{record.get('spend')}'. Using 0.")
            hist_spend_values.append(0)

        web_purchase_value = 0
        actions = record.get('action_values', [])
        if isinstance(actions, list):
            for action in actions:
                if isinstance(action, dict) and action.get('action_type') == 'web_in_store_purchase' and 'value' in action:
                    try:
                        web_purchase_value = float(action['value'])
                        break
                    except (ValueError, TypeError):
                        print(f"Warning: Invalid web_in_store_purchase value for {date_str}: '{action.get('value')}'. Using 0.")
                        web_purchase_value = 0
                        break
        hist_web_purchase_values.append(web_purchase_value)

    if not hist_dates or (not any(hist_spend_values) and not any(hist_web_purchase_values)):
        print("No valid data points for historical insights after processing.")
        return None

    print("Data processing complete for historical insights.")

    # Create DataFrame for Seaborn
    df = pd.DataFrame({
        'Date': hist_dates,
        'Spend': hist_spend_values,
        'Web In-Store Purchase': hist_web_purchase_values
    })
    df['Date'] = pd.to_datetime(df['Date'])

    # Set Seaborn style
    sns.set_style("whitegrid")

    # Create figure
    fig, ax = plt.subplots(figsize=(10, 6))

    # Plot bars
    bar_width = 0.4
    x = range(len(hist_dates))
    ax.bar([i - bar_width/2 for i in x], df['Spend'], width=bar_width/2, label='Spend (Hourly)', color='#1f77b4')
    ax.bar([i + bar_width/2 for i in x], df['Web In-Store Purchase'], width=bar_width/2, label='Revenue (Hourly)', color='#2ca02c')

    # Customize axes
    ax.set_xlabel('Date')
    ax.set_ylabel('Amount', color='#1f77b4')
    ax.set_xticks(x)
    ax.set_xticklabels([d.strftime('%Y-%m-%d') for d in df['Date']], rotation=45)
    ax.tick_params(axis='y', colors='#2ca02c')
    ax.legend(loc='upper center', bbox_to_anchor=(0.5, -0.15), ncol=2)

    # Set title
    plt.title(f"Historical Insights for {current_day_of_week or 'Unknown'} at {current_hour or 0}:00 - {current_hour or 0}:59", pad=20)

    # Adjust layout
    plt.tight_layout()

    if save_path:
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        try:
            plt.savefig(save_path, dpi=200, bbox_inches='tight')
            print(f"Historical insights plot saved as: {save_path}")
            plt.close(fig)
            return save_path
        except Exception as e:
            print(f"Error saving historical insights plot as PNG: {e}")
            html_path = save_path.replace('.png', '.html')
            plt.savefig(html_path, format='html', bbox_inches='tight')
            print(f"Fallback: Historical insights plot saved as HTML: {html_path}")
            plt.close(fig)
            return html_path
    else:
        print("Showing historical insights plot...")
        plt.close(fig)
        return None

def plot_average_order_value(daily_insights_data, save_path=None):
    """
    Plots Average Order Value (AOV) over time using Seaborn.
    Returns None if no valid data is available.
    """
    if not daily_insights_data:
        print("No daily insights data available to plot AOV.")
        return None

    daily_insights_data.sort(key=lambda x: x.get('date_start', ''))
    dates = []
    aov_values = []
    purchase_values = []
    purchase_counts = []

    print("Processing daily insights data for AOV plotting...")
    for i, day_data in enumerate(daily_insights_data):
        date_str = day_data.get('date_start')
        if not date_str:
            print(f"Warning: Skipping daily record {i} due to missing date_start: {day_data}")
            continue

        try:
            date = pd.to_datetime(date_str)
            dates.append(date)
        except ValueError:
            print(f"Warning: Skipping daily record {i} due to invalid date format: {date_str}")
            continue

        # Extract purchase value
        purchase_value = 0
        actions = day_data.get('action_values', [])
        if isinstance(actions, list):
            for action in actions:
                if isinstance(action, dict) and action.get('action_type') == 'purchase' and 'value' in action:
                    try:
                        purchase_value_str = action['value']
                        if purchase_value_str is not None:
                            purchase_value = float(purchase_value_str)
                        break
                    except (ValueError, TypeError):
                        print(f"Warning: Invalid purchase action value for {date_str}: '{action.get('value')}'. Using 0.")
                        purchase_value = 0
                        break
        purchase_values.append(purchase_value)

        # Extract purchase count - FIXED: look in the correct location and structure
        purchase_count = 0
        actions_count = day_data.get('actions', [])
        if isinstance(actions_count, list):
            for action in actions_count:
                if isinstance(action, dict) and action.get('action_type') == 'purchase' and 'value' in action:
                    try:
                        count_str = action['value']
                        if count_str is not None:
                            purchase_count = float(count_str)
                        break
                    except (ValueError, TypeError):
                        print(f"Warning: Invalid purchase action count for {date_str}: '{action.get('value')}'. Using 0.")
                        purchase_count = 0
                        break
        
        # Added this as a fallback: if no purchase count found but we have purchase value, assume count of 1
        # This gives us at least some data to plot rather than skipping entirely
        if purchase_count == 0 and purchase_value > 0:
            print(f"No purchase count found for {date_str} but purchase value exists. Using count of 1 as fallback.")
            purchase_count = 1
            
        purchase_counts.append(purchase_count)

        # Calculate AOV
        aov = purchase_value / purchase_count if purchase_count > 0 else 0
        aov_values.append(aov)
        
        # Debug output to help diagnose issues
        print(f"Date: {date_str}, Purchase Value: {purchase_value}, Count: {purchase_count}, AOV: {aov}")

    if not dates or not any(aov_values):
        print("No valid data points for AOV after processing.")
        return None

    print(f"Data processing complete for AOV insights. Found {sum(1 for v in aov_values if v > 0)} valid AOV data points.")

    # Create DataFrame for Seaborn
    df = pd.DataFrame({
        'Date': dates,
        'AOV': aov_values,
        'Purchase Value': purchase_values,
        'Purchase Count': purchase_counts
    })
    df['Date'] = pd.to_datetime(df['Date'])

    # Set Seaborn style
    sns.set_style("whitegrid")

    # Create figure
    fig, ax = plt.subplots(figsize=(10, 6))

    # Plot line for AOV
    sns.lineplot(x='Date', y='AOV', data=df, ax=ax, color='#ff9900', marker='o', linewidth=2.5)

    # Fill area under the line
    plt.fill_between(df['Date'], df['AOV'], color='#ff9900', alpha=0.2)

    # Customize axes
    ax.set_xlabel('Date')
    ax.set_ylabel('Average Order Value (₹)', fontsize=12)
    plt.xticks(rotation=45)
    
    # Add horizontal grid lines only
    ax.yaxis.grid(True)
    ax.xaxis.grid(False)
    
    # Format y-axis with currency symbol
    from matplotlib.ticker import FuncFormatter
    ax.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f'₹{x:,.2f}'))

    # Add data labels on points
    for x, y in zip(df['Date'], df['AOV']):
        ax.annotate(f'₹{y:,.0f}' if y > 0 else '', 
                   (x, y), 
                   textcoords="offset points", 
                   xytext=(0,10), 
                   ha='center',
                   fontweight='bold')

    # Set title
    plt.title(f'Average Order Value (AOV) for Last {len(dates)} Days', fontsize=14, fontweight='bold', pad=20)

    # Adjust layout to prevent overlap
    plt.tight_layout()

    if save_path:
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        try:
            plt.savefig(save_path, dpi=200, bbox_inches='tight')
            print(f"AOV plot saved as: {save_path}")
            plt.close(fig)
            return save_path
        except Exception as e:
            print(f"Error saving AOV plot as PNG: {e}")
            html_path = save_path.replace('.png', '.html')
            plt.savefig(html_path, format='html', bbox_inches='tight')
            print(f"Fallback: AOV plot saved as HTML: {html_path}")
            plt.close(fig)
            return html_path
    else:
        print("Showing AOV plot...")
        return None

def plot_hourly_aov(hourly_aov_data, save_path=None):
    """
    Plots Average Order Value (AOV) by hour of the day.
    
    Args:
        hourly_aov_data (DataFrame): DataFrame with columns 'hour', 'aov', 'total_purchases', 'total_purchase_value'
        save_path (str, optional): Path to save the plot. If None, plot is displayed but not saved.
        
    Returns:
        str or None: Path to the saved plot file, or None if not saved
    """
    if hourly_aov_data is None or hourly_aov_data.empty:
        print("No hourly AOV data available to plot.")
        return None
    
    # Make a copy to avoid modifying the original
    df = hourly_aov_data.copy()
    
    # Format hour as string for better display
    df['hour_str'] = df['hour'].apply(lambda h: f"{int(h):02d}:00")
    
    # Set Seaborn style
    sns.set_style("whitegrid")
    
    # Create figure
    fig, ax = plt.subplots(figsize=(12, 7))
    
    # Create bar plot for AOV
    bars = sns.barplot(x='hour_str', y='aov', data=df, ax=ax, color='#3498db', alpha=0.8)
    
    # Add purchase count as text on top of bars
    for i, (_, row) in enumerate(df.iterrows()):
        if row['total_purchases'] > 0:
            ax.text(i, row['aov'] + (max(df['aov']) * 0.03), 
                   f"{int(row['total_purchases'])}", 
                   ha='center', va='bottom', fontsize=8, color='#555555')
    
    # Customize axes
    ax.set_xlabel('Hour of Day', fontsize=12)
    ax.set_ylabel('Average Order Value (₹)', fontsize=12)
    plt.xticks(rotation=45)
    
    # Add horizontal grid lines only
    ax.yaxis.grid(True)
    ax.xaxis.grid(False)
    
    # Format y-axis with currency symbol
    from matplotlib.ticker import FuncFormatter
    ax.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f'₹{x:,.2f}'))
    
    # Add data labels on bars
    for i, p in enumerate(ax.patches):
        height = p.get_height()
        if height > 0:
            ax.annotate(f'₹{height:,.0f}', 
                       (p.get_x() + p.get_width() / 2., height),
                       ha='center', va='bottom', fontsize=9, fontweight='bold',
                       xytext=(0, 3), textcoords='offset points')
    
    # Add a secondary axis for purchase count
    ax2 = ax.twinx()
    sns.lineplot(x='hour_str', y='total_purchases', data=df, ax=ax2, 
                color='#e74c3c', marker='o', linewidth=2, alpha=0.7)
    ax2.set_ylabel('Number of Purchases', color='#e74c3c', fontsize=12)
    ax2.tick_params(axis='y', colors='#e74c3c')
    
    # Add a legend
    from matplotlib.lines import Line2D
    legend_elements = [
        Line2D([0], [0], color='#3498db', lw=0, marker='s', markersize=10, label='AOV (₹)', alpha=0.8),
        Line2D([0], [0], color='#e74c3c', lw=2, marker='o', markersize=6, label='Purchases')
    ]
    ax.legend(handles=legend_elements, loc='upper right')
    
    # Set title
    plt.title('Average Order Value by Hour of Day', fontsize=14, fontweight='bold', pad=20)
    
    # Adjust layout to prevent overlap
    plt.tight_layout()
    
    if save_path:
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        try:
            plt.savefig(save_path, dpi=200, bbox_inches='tight')
            print(f"Hourly AOV plot saved as: {save_path}")
            plt.close(fig)
            return save_path
        except Exception as e:
            print(f"Error saving hourly AOV plot: {e}")
            html_path = save_path.replace('.png', '.html')
            plt.savefig(html_path, format='html', bbox_inches='tight')
            print(f"Fallback: Hourly AOV plot saved as HTML: {html_path}")
            plt.close(fig)
            return html_path
    else:
        print("Showing hourly AOV plot...")
        plt.close(fig)
        return None

def plot_daily_shopify_profit(save_path=None):
    """
    Plot simple daily net profit line chart for the last 30 days.
    Uses the /api/net_profit_single_day endpoint to fetch data.
    
    API Format: /api/net_profit_single_day?startDate=YYYY-MM-DD&endDate=YYYY-MM-DD
    """
    try:
        logger.info("Plotting daily net profit for last 30 days using /api/net_profit_single_day endpoint...")
        
        # Get date range from timeframe_config
        from timeframe_config import get_timeframe_config
        tf = get_timeframe_config()
        end_date = tf['end_date']
        start_date = end_date - pd.Timedelta(days=29)  # 30 days total
        
        start_str = start_date.strftime('%Y-%m-%d')
        end_str = end_date.strftime('%Y-%m-%d')
        
        logger.info(f"Fetching net profit data from DB for last 30 days")

        db_result = fetch_net_profit_from_db(n_days=30)
        db_df = db_result.get("dailyBreakdown", pd.DataFrame())
        totals = db_result["totals"]

        if db_df.empty:
            logger.error("No net profit data returned from DB")
            return None

        # For today, DB data is partial (ad spend syncs with a lag).
        # Override today's net_profit with the real-time API value.
        ist = pytz.timezone('Asia/Kolkata')
        today_ist = datetime.now(pytz.utc).astimezone(ist).date()
        today_str = today_ist.strftime('%Y-%m-%d')
        db_df['sale_date'] = pd.to_datetime(db_df['sale_date']).dt.date
        today_mask = db_df['sale_date'] == today_ist
        if today_mask.any():
            try:
                # fetch_net_profit_single_day applies GST adjustment (ex-GST revenue), matching the daily report
                api_today = fetch_net_profit_single_day(start_date=today_str, end_date=today_str)
                api_net_profit = float(
                    (api_today.get('data') or {}).get('totals', {}).get('netProfit', 0) or 0
                )
                db_df.loc[today_mask, 'net_profit'] = api_net_profit
                logger.info(f"[Today override] DB net_profit replaced with API (ex-GST) value: {api_net_profit:.2f}")
            except Exception as e:
                logger.warning(f"[Today override] Failed to fetch API net profit for today, keeping DB value: {e}")

        logger.info(f"Fetched {len(db_df)} days of net profit data from DB")
        logger.info(f"DB totals: Revenue=₹{totals['revenue']:,.2f}, COGS=₹{totals['cogs']:,.2f}, Ad Spend=₹{totals['adSpend']:,.2f}, Net Profit=₹{totals['netProfit']:,.2f}")

        dates = []
        revenues = []
        cogs = []
        ad_spends = []
        net_profits = []

        for _, row in db_df.iterrows():
            try:
                dates.append(pd.to_datetime(row['sale_date']))
                revenues.append(float(row['revenue']))
                cogs.append(float(row['cogs']))
                ad_spends.append(float(row['total_ad_spend']))
                net_profits.append(float(row['net_profit']))
            except (ValueError, KeyError) as e:
                logger.warning(f"Skipping invalid DB row: {row.to_dict()}, error: {e}")
                continue

        if not dates:
            logger.error("No valid data points for net profit plotting")
            return None

        df = pd.DataFrame({
            'Date': dates,
            'Revenue': revenues,
            'COGS': cogs,
            'Ad Spend': ad_spends,
            'Net Profit': net_profits
        })
        
        # Convert dates list to pandas Series for proper indexing in plots
        dates_series = pd.Series(dates)
        
        # Set Seaborn style
        sns.set_style("whitegrid")
        
        # Create simple figure with single plot
        fig, ax = plt.subplots(figsize=(12, 6))
        
        # Plot net profit line with color-coded segments (green for positive/zero, red for negative)
        # Plot line in segments: green for >=0, red for <0, handle zero crossings
        for i in range(1, len(dates_series)):
            x0, x1 = dates_series.iloc[i-1], dates_series.iloc[i]
            y0, y1 = net_profits[i-1], net_profits[i]
            color = '#2ecc71' if y0 >= 0 and y1 >= 0 else 'red' if y0 < 0 and y1 < 0 else None
            if color:
                ax.plot([x0, x1], [y0, y1], color=color, linewidth=1.35, solid_capstyle='round', zorder=1)
            else:
                # If the line crosses zero, split at zero for color change
                if y0 < 0 and y1 >= 0 or y0 >= 0 and y1 < 0:
                    # Find the zero crossing point
                    if y1 != y0:
                        x_cross = x0 + (x1 - x0) * (0 - y0) / (y1 - y0)
                        ax.plot([x0, x_cross], [y0, 0], color='red' if y0 < 0 else '#2ecc71', linewidth=1.35, solid_capstyle='round', zorder=1)
                        ax.plot([x_cross, x1], [0, y1], color='red' if y1 < 0 else '#2ecc71', linewidth=1.35, solid_capstyle='round', zorder=1)
        
        # Highlight points with color coding: green for positive/zero, red for negative
        neg = df['Net Profit'] < 0
        pos = df['Net Profit'] >= 0
        ax.scatter(dates_series[pos], df['Net Profit'][pos], color='#2ecc71', marker='o', s=38, linewidths=0.6, edgecolors='#1e8449', zorder=2)
        ax.scatter(dates_series[neg], df['Net Profit'][neg], color='red', marker='o', s=38, linewidths=0.6, edgecolors='#922b21', zorder=2)
        
        avg_net_profit = float(df['Net Profit'].mean())
        threshold_top = 80000.0
        # Slim reference guides: full-span, fine dash, readable without dominating the series
        threshold_line_kw = dict(
            color='#4b5563',
            linewidth=1.05,
            linestyle=(0, (1, 3)),
            alpha=0.9,
            zorder=2.5,
        )
        ax.axhline(threshold_top, **threshold_line_kw)
        ax.axhline(avg_net_profit, **threshold_line_kw)
        y_min = min(float(df['Net Profit'].min()), avg_net_profit, threshold_top)
        y_max = max(float(df['Net Profit'].max()), avg_net_profit, threshold_top)
        span = y_max - y_min
        pad = span * 0.05 if span > 0 else (abs(y_max) * 0.05 if y_max != 0 else 1000.0)
        ax.set_ylim(y_min - pad, y_max + pad)
        ax.set_xmargin(0)
        
        # Format axes
        ax.set_xlabel('Date', fontsize=12)
        ax.set_ylabel('Net Profit (Rs)', fontsize=12)
        ax.set_title(f'Daily Net Profit ({start_str} to {end_str})', fontsize=14, fontweight='bold', pad=20)
        
        # Format y-axis with currency (Rs instead of ₹)
        from matplotlib.ticker import FuncFormatter
        ax.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f'Rs{x:,.0f}'))
        
        # Format x-axis - show every day
        ax.xaxis.set_major_locator(mdates.DayLocator(interval=1))
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%d %b'))
        plt.setp(ax.xaxis.get_majorticklabels(), rotation=90)
        
        # Add grid (light so the series and reference lines stay primary)
        ax.grid(True, alpha=0.22, linewidth=0.65, linestyle='-')
        ax.set_axisbelow(True)
        
        # Add data labels on significant points (like Rs626 and Rs-4,434 in the image)
        for x, y in zip(df['Date'], df['Net Profit']):
            # Show labels for non-zero values or significant positive values
            if abs(y) > 500 or (y > 0 and y != 0):
                ax.annotate(f'Rs{y:,.0f}', (x, y), textcoords="offset points", 
                           xytext=(0, 10), ha='center', fontsize=9, color='black',
                           bbox=dict(boxstyle="round,pad=0.2", facecolor='white', alpha=0.8))
        
        plt.tight_layout()
        
        if save_path:
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            plt.savefig(save_path, dpi=200, bbox_inches='tight')
            logger.info(f"Daily net profit plot saved as: {save_path}")
            plt.close(fig)
            return save_path
        else:
            plt.close(fig)
            return None
            
    except Exception as e:
        logger.error(f"Error plotting daily net profit: {str(e)}", exc_info=True)
        return None

def plot_hourly_sales_last_7_days(save_path=None):
    """
    Plots a line graph of total sales (sum of total_price_amount) by hour for each of the last 7 days (including today)
    from the shopify_orders table, excluding cancelled orders. Shows individual daily sum lines (unique color, labeled, faded) and the mean (average) line (bold black).
    All lines are continuous from 00 to 23 (missing hours are filled with zero).
    The y-axis is autoscaled (not capped). Uses a dynamic date range for the last 7 days.
    """
    try:
        logger.info("Plotting sum of hourly sales for individual days and mean for average line for last 7 days from shopify_orders...")
        conn_str = (
            f"postgresql://{DB_CONFIG['user']}:{DB_CONFIG['password']}@"
            f"{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}?sslmode=require"
        )
        engine = create_engine(conn_str, isolation_level="AUTOCOMMIT")
        # Calculate dynamic date range for last 7 days (including today)
        end_date = pd.Timestamp.now(tz=INDIAN_TZ).replace(hour=23, minute=59, second=59, microsecond=0)
        start_date = (end_date - pd.Timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
        # Query for the relevant data, excluding cancelled orders
        query = f'''
            SELECT
                total_price_amount,
                created_at_ist
            FROM
                shopify_orders
            WHERE
                created_at_ist >= '{start_date.strftime('%Y-%m-%d %H:%M:%S%z')}'
                AND created_at_ist <= '{end_date.strftime('%Y-%m-%d %H:%M:%S%z')}'
                AND cancelled_at_ist IS NULL
            ORDER BY created_at_ist DESC
        '''
        with engine.connect() as conn:
            df = pd.read_sql(query, conn)
        if df.empty:
            logger.warning("No shopify_orders data found for the last 7 days.")
            return None
        if 'total_price_amount' in df.columns:
            df['total_price_amount'] = apply_net_revenue_column(df['total_price_amount'])
        # Parse datetime and extract hour and date
        df['created_at_ist'] = pd.to_datetime(df['created_at_ist'])
        df['hour'] = df['created_at_ist'].dt.hour
        df['date'] = df['created_at_ist'].dt.date
        # Compute sum of sales per hour for each day
        daily_hourly = df.groupby(['date', 'hour'])['total_price_amount'].sum().reset_index()
        # Ensure every day has all 24 hours (fill missing with zero)
        all_hours = pd.Series(range(24), name='hour')
        all_dates = pd.Series(sorted(daily_hourly['date'].unique()), name='date')
        full_index = pd.MultiIndex.from_product([all_dates, all_hours], names=['date', 'hour'])
        daily_hourly_full = daily_hourly.set_index(['date', 'hour']).reindex(full_index, fill_value=0).reset_index()
        # Pivot for plotting individual days
        pivot = daily_hourly_full.pivot(index='hour', columns='date', values='total_price_amount')
        # Compute mean (average) per hour across all days
        hourly_mean = pivot.mean(axis=1)
        # Plot
        sns.set_style("whitegrid")
        fig, ax = plt.subplots(figsize=(14, 7))
        # Plot individual days in unique colors and label each date, with more faded lines
        color_palette = sns.color_palette('tab10', n_colors=len(pivot.columns))
        for i, (date, color) in enumerate(zip(pivot.columns, color_palette)):
            ax.plot(pivot.index, pivot[date], color=color, alpha=0.3, linewidth=2, label=str(date))
        # Plot mean line in bold black
        ax.plot(hourly_mean.index, hourly_mean.values, color='black', linewidth=2.5, linestyle='-', marker='o', label='Average (7 days)')
        ax.set_xlabel('Hour of Day', fontsize=12)
        ax.set_ylabel('Total Sales (₹)', fontsize=12)
        ax.set_xticks(range(24))
        ax.set_xticklabels([f"{h:02d}" for h in range(24)])
        ax.set_title('Hourly Sales for Last 7 Days', fontsize=15, fontweight='bold', pad=20)
        ax.legend(loc='upper left', title='Date')
        from matplotlib.ticker import FuncFormatter
        ax.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f'₹{x:,.0f}'))
        plt.tight_layout()
        if save_path:
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            plt.savefig(save_path, dpi=200, bbox_inches='tight')
            logger.info(f"Hourly sales plot saved as: {save_path}")
            plt.close(fig)
            return save_path
        else:
            plt.show()
            plt.close(fig)
            return None
    except Exception as e:
        logger.error(f"Error plotting hourly sales: {str(e)}", exc_info=True)
        return None
    finally:
        if 'engine' in locals():
            engine.dispose()
            logger.info("Database connection closed")


def _build_sales_by_state_plot_data(start_date_str, end_date_str, other_share_threshold=0.02):
    """
    Fetch Shopify sales by state, aggregate small states into 'Other', return plot frame and summary stats.
    Used by bar and pie charts.
    """
    raw = fetch_shopify_sales_by_state(start_date_str, end_date_str)
    if raw.empty:
        logger.warning("No sales-by-state data for chart.")
        return None
    raw = raw.copy()
    raw["total_sales"] = pd.to_numeric(raw["total_sales"], errors="coerce").fillna(0)
    if "order_count" not in raw.columns:
        raw["order_count"] = 0
    raw["order_count"] = pd.to_numeric(raw["order_count"], errors="coerce").fillna(0).astype(int)
    raw = raw.sort_values("total_sales", ascending=False).reset_index(drop=True)

    grand_total_sales = float(raw["total_sales"].sum())
    grand_total_orders = int(raw["order_count"].sum())
    if grand_total_sales <= 0:
        return None

    n_states_raw = len(raw)
    top_by_sales = raw.iloc[0]["state"]
    top_sales_amt = float(raw.iloc[0]["total_sales"])
    idx_max_orders = raw["order_count"].idxmax()
    top_by_orders = raw.loc[idx_max_orders, "state"]
    top_orders_ct = int(raw.loc[idx_max_orders, "order_count"])
    top3_share_pct = float(raw.head(3)["total_sales"].sum()) / grand_total_sales * 100

    raw["share"] = raw["total_sales"] / grand_total_sales
    main_mask = raw["share"] >= other_share_threshold
    main_df = raw[main_mask].copy()
    small_df = raw[~main_mask]
    if not small_df.empty:
        other_row = pd.DataFrame(
            [
                {
                    "state": "Other",
                    "total_sales": float(small_df["total_sales"].sum()),
                    "order_count": int(small_df["order_count"].sum()),
                }
            ]
        )
        plot_df = pd.concat([main_df.drop(columns=["share"]), other_row], ignore_index=True)
    else:
        plot_df = main_df.drop(columns=["share"]).copy()

    plot_df = plot_df.sort_values("total_sales", ascending=False).reset_index(drop=True)
    plot_df["share_pct"] = plot_df["total_sales"] / grand_total_sales * 100

    try:
        end_dt = datetime.strptime(end_date_str, "%Y-%m-%d")
        title_date = end_dt.strftime("%d %b %Y")
    except ValueError:
        title_date = end_date_str

    return {
        "plot_df": plot_df,
        "grand_total_sales": grand_total_sales,
        "grand_total_orders": grand_total_orders,
        "n_states_raw": n_states_raw,
        "top_by_sales": top_by_sales,
        "top_sales_amt": top_sales_amt,
        "top_by_orders": top_by_orders,
        "top_orders_ct": top_orders_ct,
        "top3_share_pct": top3_share_pct,
        "title_date": title_date,
    }


def plot_sales_by_state_chart(start_date_str, end_date_str, save_path=None, other_share_threshold=0.02):
    """
    Horizontal bar chart: sales by state with order counts, shares, summary panel.
    Groups states with sales share below other_share_threshold into 'Other'.
    """
    try:
        built = _build_sales_by_state_plot_data(
            start_date_str, end_date_str, other_share_threshold=other_share_threshold
        )
        if not built:
            return None
        plot_df = built["plot_df"]
        grand_total_sales = built["grand_total_sales"]
        grand_total_orders = built["grand_total_orders"]
        n_states_raw = built["n_states_raw"]
        top_by_sales = built["top_by_sales"]
        top_sales_amt = built["top_sales_amt"]
        top_by_orders = built["top_by_orders"]
        top_orders_ct = built["top_orders_ct"]
        top3_share_pct = built["top3_share_pct"]
        title_date = built["title_date"]

        n_bars = len(plot_df)
        row_h = 0.42
        fig_h = max(7.0, 2.8 + n_bars * row_h)
        fig = plt.figure(figsize=(14, fig_h), facecolor="white")
        gs = fig.add_gridspec(1, 2, width_ratios=[2.35, 1.0], wspace=0.22)
        ax = fig.add_subplot(gs[0, 0])
        ax_summary = fig.add_subplot(gs[0, 1])

        top_color = "#0f4c81"
        n_rest = max(n_bars - 1, 1)
        other_colors = plt.cm.Blues(np.linspace(0.42, 0.72, n_rest))
        bar_colors = [top_color]
        for i in range(1, n_bars):
            bar_colors.append(other_colors[(i - 1) % len(other_colors)])

        y_pos = np.arange(n_bars)
        sales_vals = plot_df["total_sales"].values
        ax.barh(y_pos, sales_vals, height=0.65, color=bar_colors, edgecolor="white", linewidth=0.8)
        ax.set_yticks(y_pos)
        ax.set_yticklabels(plot_df["state"], fontsize=10)
        ax.invert_yaxis()
        ax.set_xlabel("Total sales (₹)", fontsize=11, color="#333333")
        ax.tick_params(axis="x", labelsize=9, colors="#333333")
        ax.tick_params(axis="y", labelsize=10, colors="#333333")
        ax.set_facecolor("white")
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
        ax.spines["left"].set_color("#cccccc")
        ax.spines["bottom"].set_color("#cccccc")
        ax.grid(axis="x", alpha=0.28, linestyle="-", color="#bbbbbb")
        ax.set_axisbelow(True)

        xmax = max(sales_vals) * 1.0
        label_pad = xmax * 0.012
        max_sales = float(sales_vals.max()) if len(sales_vals) else 0
        for yi, (_, row) in enumerate(plot_df.iterrows()):
            x = float(row["total_sales"])
            oc = int(row["order_count"])
            sh = float(row["share_pct"])
            label = f"₹{x:,.0f} | Orders: {oc} | Share: {sh:.1f}%"
            fs = 8.2 if len(label) < 72 else 7.2
            ax.text(x + label_pad, yi, label, va="center", ha="left", fontsize=fs, color="#222222")

        ax.set_xlim(0, max_sales * 1.62 if max_sales > 0 else 1)

        try:
            start_dt = datetime.strptime(start_date_str, "%Y-%m-%d")
            period_start = start_dt.strftime("%d %b %Y")
        except ValueError:
            period_start = start_date_str
        if start_date_str == end_date_str:
            fig.suptitle(
                f"Sales by state — {title_date}",
                fontsize=15,
                fontweight="bold",
                color="#1a1a1a",
                y=0.98,
            )
            period_line = title_date
        else:
            fig.suptitle(
                f"Sales by state — {period_start} to {title_date}",
                fontsize=15,
                fontweight="bold",
                color="#1a1a1a",
                y=0.98,
            )
            period_line = f"{period_start} – {title_date}"
        sub = (
            f"Period: {period_line}  |  Total sales: ₹{grand_total_sales:,.0f}  |  "
            f"Total orders: {grand_total_orders:,}"
        )
        fig.text(0.5, 0.935, sub, ha="center", fontsize=11, color="#444444")

        ax_summary.set_facecolor("#f7f9fc")
        ax_summary.set_xticks([])
        ax_summary.set_yticks([])
        for spine in ax_summary.spines.values():
            spine.set_visible(False)

        summary_lines = [
            "Summary",
            "",
            f"1. Top state by sales: {top_by_sales}",
            f"   ₹{top_sales_amt:,.0f}",
            "",
            f"2. Top state by orders: {top_by_orders}",
            f"   {top_orders_ct:,} orders",
            "",
            f"3. Total states: {n_states_raw}",
            "",
            "4. Top 3 states — share of total sales:",
            f"   {top3_share_pct:.1f}%",
        ]
        summary_text = "\n".join(summary_lines)
        ax_summary.text(
            0.08,
            0.95,
            summary_text,
            transform=ax_summary.transAxes,
            fontsize=10,
            verticalalignment="top",
            fontfamily="sans-serif",
            color="#222222",
            bbox=dict(boxstyle="round,pad=0.55", facecolor="white", edgecolor="#d0d7de", linewidth=1),
        )

        plt.tight_layout(rect=[0.02, 0.05, 0.98, 0.88])
        if save_path:
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            plt.savefig(save_path, dpi=200, bbox_inches="tight", facecolor="white")
            plt.close(fig)
            return save_path
        plt.close(fig)
        return None
    except Exception as e:
        logger.error(f"Error plotting sales by state chart: {e}", exc_info=True)
        return None


def plot_sales_by_state_pie_chart(start_date_str, end_date_str, save_path=None, other_share_threshold=0.02):
    """
    Pie chart: sales share by state for the given date range (use same start/end for a single day).
    Same 'Other' grouping as the bar chart. Wedges show state names (and % on larger slices);
    legend lists sales, orders, and share.
    """
    try:
        built = _build_sales_by_state_plot_data(
            start_date_str, end_date_str, other_share_threshold=other_share_threshold
        )
        if not built:
            return None
        plot_df = built["plot_df"]
        grand_total_sales = built["grand_total_sales"]
        grand_total_orders = built["grand_total_orders"]
        n_states_raw = built["n_states_raw"]
        title_date = built["title_date"]

        n = len(plot_df)
        sizes = plot_df["total_sales"].values.astype(float)
        top_color = "#0f4c81"
        if n <= 1:
            colors = [top_color]
        else:
            other_colors = plt.cm.Blues(np.linspace(0.42, 0.72, n - 1))
            colors = [top_color] + [other_colors[i % len(other_colors)] for i in range(n - 1)]

        wedge_labels = []
        wedge_label_fontsizes = []
        for _, row in plot_df.iterrows():
            st = str(row["state"])
            sh = float(row["share_pct"])
            if sh >= 5.0:
                wedge_labels.append(f"{st}\n{sh:.1f}%")
                wedge_label_fontsizes.append(10)
            elif sh >= 2.5:
                wedge_labels.append(f"{st}\n{sh:.1f}%")
                wedge_label_fontsizes.append(9)
            else:
                wedge_labels.append(st)
                wedge_label_fontsizes.append(8)

        single_day = start_date_str == end_date_str
        try:
            start_dt = datetime.strptime(start_date_str, "%Y-%m-%d")
            period_start = start_dt.strftime("%d %b %Y")
        except ValueError:
            period_start = start_date_str
        if single_day:
            range_note = title_date
            period_line = title_date
        else:
            range_note = f"{period_start} to {title_date}"
            period_line = f"{period_start} – {title_date}"
        sub = (
            f"Period: {period_line}  |  Total sales: ₹{grand_total_sales:,.0f}  |  "
            f"Total orders: {grand_total_orders:,}  |  States: {n_states_raw}  |  Slices: {n}"
        )

        # Match plot_hourly_sales_last_7_days: 14×7 in, dpi 200, title 15 bold, caption 12 (axis-label style).
        fig = plt.figure(figsize=(14, 7), facecolor="white")
        fig.subplots_adjust(left=0.06, right=0.94, top=0.86, bottom=0.22)
        ax_pie = fig.add_axes([0.1, 0.26, 0.8, 0.52])
        fig.text(
            0.5,
            0.945,
            f"Sales by state — {range_note}",
            ha="center",
            fontsize=15,
            fontweight="bold",
            color="#1a1a1a",
        )
        fig.text(0.5, 0.905, sub, ha="center", fontsize=12, color="#333333")

        # When autopct is None, matplotlib's pie() returns only (wedges, texts), not three values.
        _pie_ret = ax_pie.pie(
            sizes,
            labels=wedge_labels,
            autopct=None,
            startangle=90,
            counterclock=False,
            colors=colors,
            radius=1.0,
            wedgeprops=dict(edgecolor="white", linewidth=0.9),
            textprops=dict(color="#333333", fontsize=10),
            labeldistance=1.12,
            rotatelabels=True,
        )
        wedges, texts = _pie_ret[0], _pie_ret[1]
        for t, fs in zip(texts, wedge_label_fontsizes):
            t.set_fontsize(fs)
            t.set_fontweight("normal")
            t.set_color("#333333")

        ax_pie.set_aspect("equal")
        ax_pie.set_facecolor("white")

        legend_rows = [
            f"{row['state']}: ₹{float(row['total_sales']):,.0f} | {int(row['order_count'])} ord. | {float(row['share_pct']):.1f}%"
            for _, row in plot_df.iterrows()
        ]
        ncol = 2 if n > 6 else 1
        fig.legend(
            wedges,
            legend_rows,
            title="By state (sales | orders | share)",
            loc="lower center",
            bbox_to_anchor=(0.5, 0.02),
            ncol=ncol,
            fontsize=10,
            title_fontsize=11,
            frameon=True,
            fancybox=True,
            framealpha=0.95,
        )

        if save_path:
            d = os.path.dirname(save_path)
            if d:
                os.makedirs(d, exist_ok=True)
            plt.savefig(save_path, dpi=200, bbox_inches="tight", facecolor="white", pad_inches=0.2)
            plt.close(fig)
            return save_path
        plt.close(fig)
        return None
    except Exception as e:
        logger.error(f"Error plotting sales by state pie chart: {e}", exc_info=True)
        return None


# --- Function to Generate Plots for Email Attachments ---
def generate_plots_for_email(
    days=DAYS_TO_FETCH,
    months=MONTHS_FOR_HISTORICAL,
    report_dir=REPORT_DIR,
    hourly_aov_data=None,
    report_day_str=None,
):
    # Get current time in Indian timezone for file naming
    now = datetime.now(INDIAN_TZ)
    logger.info("Starting to generate plots for email...")
    from timeframe_config import get_timeframe_config

    tf_report = get_timeframe_config()
    # Sales-by-state pie: use the same calendar day as the report when provided (avoids mismatch with get_timeframe_config() defaults).
    if report_day_str:
        sales_range_end = str(report_day_str).strip()[:10]
    else:
        end_d = tf_report["end_date"]
        sales_range_end = (
            end_d.strftime("%Y-%m-%d") if hasattr(end_d, "strftime") else str(end_d)[:10]
        )
    logger.info("Sales-by-state pie chart date: %s", sales_range_end)
    # Firebase token initialization is now handled automatically by api_data_fetcher
    # It will check env vars, load from file, or auto-generate from credentials
    # Detailed logging is available in api_data_fetcher logs for diagnostics
    
    # Load Facebook API credentials from global config
    ACCESS_TOKEN = get_global_config("Socialpepper_FB_ACCESS_TOKEN")
    ACCOUNT_ID = get_global_config("Socialpepper_FB_AD_ACCOUNT_ID")

    if not ACCESS_TOKEN or not ACCOUNT_ID:
        logger.error("Socialpepper_FB_ACCESS_TOKEN or Socialpepper_FB_AD_ACCOUNT_ID not set in database.")
        sys.exit("Socialpepper_FB_ACCESS_TOKEN or Socialpepper_FB_AD_ACCOUNT_ID not set in database.")

    daily_insights_data, daily_error = fetch_daily_insights_from_api(days, ACCESS_TOKEN, ACCOUNT_ID)

    plot_files = []
    today = datetime.now().strftime('%Y-%m-%d')

    # Create report directory with timestamp in Indian timezone
    timestamp = now.strftime('%Y%m%d_%H%M%S')
    report_path = os.path.join(report_dir, timestamp)
    os.makedirs(report_path, exist_ok=True)
    logger.info(f"Created report directory: {report_path}")

    # Plot Daily Insights
    if daily_insights_data:
        plot_file = os.path.join(report_path, f'daily_insights_{today}.png')
        saved_path = plot_daily_amounts(daily_insights_data, save_path=plot_file)
        if saved_path and saved_path.endswith('.png'):
            plot_files.append(saved_path)
            logger.info(f"Daily insights plot saved to: {saved_path}")
    else:
        logger.error(f"Could not plot daily insights due to fetching error: {daily_error}")

    # Plot Daily Shopify Profit (line graph)
    logger.info("Generating daily Shopify profit plot...")
    plot_file = os.path.join(report_path, f'daily_shopify_profit_{today}.png')
    saved_path = plot_daily_shopify_profit(save_path=plot_file)
    if saved_path and saved_path.endswith('.png'):
        plot_files.append(saved_path)
        logger.info(f"Daily Shopify profit plot saved to: {saved_path}")
    else:
        logger.error("Failed to generate daily Shopify profit plot")
        
    # Plot Hourly Sales for Last 7 Days
    logger.info("Generating hourly sales plot for last 7 days...")
    plot_file = os.path.join(report_path, f'hourly_sales_last_7_days_{today}.png')
    saved_path = plot_hourly_sales_last_7_days(save_path=plot_file)
    if saved_path and saved_path.endswith('.png'):
        plot_files.append(saved_path)
        logger.info(f"Hourly sales plot saved to: {saved_path}")
    else:
        logger.error("Failed to generate hourly sales plot")

    logger.info(
        "Generating sales-by-state pie chart (report day) for %s...",
        sales_range_end,
    )
    plot_file = os.path.join(
        report_path, f"sales_by_state_pie_current_day_{sales_range_end}.png"
    )
    saved_path = plot_sales_by_state_pie_chart(
        sales_range_end, sales_range_end, save_path=plot_file
    )
    if saved_path and saved_path.endswith(".png"):
        plot_files.append(saved_path)
        logger.info(f"Sales by state pie chart saved to: {saved_path}")
    else:
        logger.warning("Sales by state pie chart not generated (no data or error)")

    logger.info(
        "Generating daily channel performance chart for %s...",
        sales_range_end,
    )
    plot_file = os.path.join(
        report_path, f"channel_performance_daily_{sales_range_end}.png"
    )
    saved_path = plot_channel_performance_daily(
        sales_range_end, save_path=plot_file
    )
    if saved_path and saved_path.endswith(".png"):
        plot_files.append(saved_path)
        logger.info(f"Channel performance chart saved to: {saved_path}")
    else:
        logger.warning("Channel performance chart not generated (no data or error)")

    # Plot Hourly AOV if data is provided
    if hourly_aov_data is not None and not hourly_aov_data.empty:
        plot_file = os.path.join(report_path, f'hourly_aov_{today}.png')
        saved_path = plot_hourly_aov(hourly_aov_data, save_path=plot_file)
        if saved_path and saved_path.endswith('.png'):
            plot_files.append(saved_path)
            logger.info(f"Hourly AOV plot saved to: {saved_path}")
    else:
        logger.info("No hourly AOV data provided for plotting")

    # Generate AOV insights plot for display if daily data is available
    if daily_insights_data:
        logger.info("Generating AOV insights plot for display...")
        plot_average_order_value(daily_insights_data)

    if not daily_insights_data and hourly_aov_data is None:
        logger.error(f"Failed to fetch data: Daily error: {daily_error}")
        sys.exit(f"Failed to fetch data: Daily error: {daily_error}")

    logger.info(f"Generated {len(plot_files)} plots for email")
    return plot_files

# --- Placeholder for Email Sending Function ---
def send_email_with_attachments(recipient_email, subject, body, attachment_paths):
    """
    Placeholder function to send an email with PNG attachments.
    Implement this using smtplib and email libraries based on your email server settings.

    Args:
        recipient_email (str): Email address of the recipient.
        subject (str): Email subject line.
        body (str): Email body text.
        attachment_paths (list): List of file paths to PNG attachments.

    Example implementation:
    ```
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.image import MIMEImage

    msg = MIMEMultipart()
    msg['From'] = 'your_email@example.com'
    msg['To'] = recipient_email
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain'))

    for path in attachment_paths:
        # Use the path as-is if it's absolute, otherwise use REPORT_DIR
        file_path = path if os.path.isabs(path) else os.path.join(REPORT_DIR, os.path.basename(path))
        with open(file_path, 'rb') as f:
            img = MIMEImage(f.read(), name=os.path.basename(path))
            msg.attach(img)

    with smtplib.SMTP('smtp.example.com', 587) as server:
        server.starttls()
        server.login('your_email@example.com', 'your_password')
        server.send_message(msg)
    ```
    """
    print(f"Placeholder: Would send email to {recipient_email} with subject '{subject}' and attachments: {attachment_paths}")
    print(f"Email body: {body}")
    print("Please implement send_email_with_attachments with your email server settings.")
    return True  # Simulate success for testing

# --- Main Execution ---
if __name__ == "__main__":
    # Generate plots and get PNG file paths
    plot_files = generate_plots_for_email()

    # Example: Simulate sending email with attachments
    if plot_files:
        recipient = "recipient@example.com"
        subject = "Daily and Historical Marketing Insights"
        body = "Attached are the latest marketing insights plots."
        send_email_with_attachments(recipient, subject, body, plot_files)
    else:
        print("No plots generated, skipping email.", file=sys.stderr)

    # Optionally display plots for testing
    # daily_insights_data, daily_error = fetch_daily_insights_from_api(DAYS_TO_FETCH, ACCESS_TOKEN, ACCOUNT_ID)
    # historical_data, current_day_of_week, current_hour, historical_error = fetch_historical_hourly_insights_from_api(MONTHS_FOR_HISTORICAL, ACCESS_TOKEN, ACCOUNT_ID)

    # if daily_insights_data:
    #     print("Generating daily insights plot for display...")
    #     plot_daily_amounts(daily_insights_data)
    # else:
    #     print(f"Could not plot daily insights due to fetching error: {daily_error}", file=sys.stderr)

    # if historical_data:
    #     print("Generating historical insights plot for display...")
    #     plot_historical_insights(historical_data, current_day_of_week, current_hour)
    # else:
    #     print(f"Could not plot historical insights due to fetching error: {historical_error}", file=sys.stderr)

    # if daily_insights_data:
    #     print("Generating AOV insights plot for display...")
    #     plot_average_order_value(daily_insights_data)

    # if not daily_insights_data:
    #     sys.exit(f"Failed to fetch data: Daily error: {daily_error}")
