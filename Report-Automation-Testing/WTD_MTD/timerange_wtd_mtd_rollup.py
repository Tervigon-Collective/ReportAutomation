import json
import os
import sys
from datetime import datetime, timedelta
import pandas as pd
import xlsxwriter
import numpy as np
import pytz
import msal
import base64
import requests
import logging
from dotenv import load_dotenv

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from timeframe_config import get_timeframe_config
from api_data_fetcher import fetch_marketing_hourly, fetch_google_spend, fetch_amazon_data, fetch_product_profitability
from global_config import get_global_config, get_temp_dir, get_report_dir

# Import functions from dailyrollup.py
from dailyrollup import (
    order_columns_by_funnel, SKU_FIELDS, round_for_output,
    parse_product_details, explode_skus, build_ad_sku_rollup,
    transform_attribution_data, _merge_repeating_values_in_sheet,
    run as run_daily_rollup,
    get_campaign_data, get_meta_funnel_metrics, get_campaign_grand_total_for_pdf
)

# Import PDF generation functions
from api_data_fetcher import get_organized_metrics_for_pdf
from excel_generation import generate_pdf_report, get_google_funnel_metrics

# Amazon (ClickHouse gold) sheets for the WTD/MTD report
from amazon_entity_report import add_amazon_sheets_for_timeframe, get_amazon_clickhouse_summary

# Set up logging
_temp_dir = get_temp_dir()
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(_temp_dir, 'wtd_mtd_report.log'), encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Set timezone to IST
IST = pytz.timezone('Asia/Kolkata')

# Load environment variables
load_dotenv()

# Email configuration from environment variables
CLIENT_ID = get_global_config('AZURE_CLIENT_ID')
CLIENT_SECRET = get_global_config('AZURE_CLIENT_SECRET')
TENANT_ID = get_global_config('AZURE_TENANT_ID')
EMAIL_SENDER = os.getenv('EMAIL_SENDER', '')
EMAIL_RECIPIENTS = os.getenv('EMAIL_RECIPIENTS', '').split(',')

# Validate email configuration
if not all([CLIENT_ID, CLIENT_SECRET, TENANT_ID, EMAIL_SENDER, EMAIL_RECIPIENTS[0]]):
    logger.warning("Email configuration incomplete - email sending will be disabled")
    EMAIL_ENABLED = False
else:
    EMAIL_ENABLED = True
    logger.info(f"Email configuration loaded successfully. Sender: {EMAIL_SENDER}, Recipients: {len(EMAIL_RECIPIENTS)}")

def get_wtd_mtd_timeframes():
    """
    Calculate Week-to-Date (WTD) and Month-to-Date (MTD) timeframes.
    Returns dict with 'wtd' and 'mtd' keys, each containing start_date and end_date.

    WTD: Monday of the current week to today (calendar week, consistent with Amazon).
    MTD: 1st of the current month to today.
    Daily: Previous day only.
    """
    now = datetime.now(IST)

    # Week-to-Date: Monday of the current week to today
    wtd_end = now.replace(hour=23, minute=59, second=59, microsecond=0)
    wtd_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)

    # Month-to-Date: 1st of current month to today
    mtd_end = now.replace(hour=23, minute=59, second=59, microsecond=0)
    mtd_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    
    # Daily: Previous day only (for daily entity report sheet)
    daily_date = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    daily_end = daily_date.replace(hour=23, minute=59, second=59, microsecond=0)
    
    return {
        'wtd': {
            'start_date': wtd_start,
            'end_date': wtd_end,
            'label': 'Week-to-Date'
        },
        'mtd': {
            'start_date': mtd_start,
            'end_date': mtd_end,
            'label': 'Month-to-Date'
        },
        'daily': {
            'start_date': daily_date,
            'end_date': daily_end,
            'label': 'Daily'
        }
    }


def get_amazon_calendar_timeframes():
    """
    Calendar-based WTD/MTD for Amazon (per business requirement).

    - WTD: start of current week (Monday) -> today (end of day); callers apply
      `days_lag=1` (or subtract 1 day) so the effective window ends yesterday.
    - MTD: 1st of current month -> yesterday (end of day)

    Amazon gold tables lag ~1 day, so MTD must not include today.

    To switch the week to start on Sunday, change `now.weekday()` below to
    `(now.weekday() + 1) % 7`.
    """
    now = datetime.now(IST)
    today_end = now.replace(hour=23, minute=59, second=59, microsecond=0)
    yesterday_end = (now - timedelta(days=1)).replace(
        hour=23, minute=59, second=59, microsecond=0
    )
    # Monday-start week: weekday() == 0 on Monday, 6 on Sunday.
    wtd_start = (
        (today_end - timedelta(days=now.weekday()))
        .replace(hour=0, minute=0, second=0, microsecond=0)
    )
    mtd_start = today_end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return {
        'wtd': {
            'start_date': wtd_start,
            'end_date': today_end,
            'label': 'Week-to-Date (calendar, Mon-today)',
        },
        'mtd': {
            'start_date': mtd_start,
            'end_date': yesterday_end,
            'label': 'Month-to-Date (calendar, 1st-yesterday)',
        },
    }


def _create_unified_funnel_columns(df: pd.DataFrame, source_type: str) -> pd.DataFrame:
    """
    Create unified funnel columns for Meta/Google/Organic data (same logic as dailyrollup.py).
    Handles offsite/onsite pixel data and creates consistent funnel metrics.
    """
    if df.empty:
        return df
    
    # Define funnel action columns
    off_atc = 'action_offsite_pixel_add_to_cart'
    on_atc = 'action_onsite_web_add_to_cart'
    off_ic = 'action_offsite_pixel_initiate_checkout'
    on_ic = 'action_onsite_web_initiate_checkout'
    lp_view = 'action_landing_page_view'
    
    # Landing page views (no offsite/onsite distinction, take as-is if present)
    df['_landing_page_view'] = df.get(lp_view, pd.Series([0]*len(df))).fillna(0)
    
    # Add to cart (prefer offsite when > 0, else onsite)
    df['_add_to_cart'] = (
        df.get(off_atc, pd.Series([pd.NA]*len(df))).fillna(0)
            .where(df.get(off_atc, pd.Series([0]*len(df))).fillna(0) > 0,
                   df.get(on_atc, pd.Series([0]*len(df))).fillna(0))
    )
    
    # Initiate checkout (prefer offsite when > 0, else onsite)
    df['_initiate_checkout'] = (
        df.get(off_ic, pd.Series([pd.NA]*len(df))).fillna(0)
            .where(df.get(off_ic, pd.Series([0]*len(df))).fillna(0) > 0,
                   df.get(on_ic, pd.Series([0]*len(df))).fillna(0))
    )
    
    # Ensure funnel columns are numeric
    for col in ['_landing_page_view', '_add_to_cart', '_initiate_checkout']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
    
    return df

def build_campaign_rollup_with_sku(df: pd.DataFrame, timeframe_label: str, source_type: str) -> pd.DataFrame:
    """
    Build campaign/adset/ad-level rollup with SKU attribution for WTD/MTD reports.
    For Meta/Google: Aggregates by campaign hierarchy (campaign > adset > ad)
    For Organic: Aggregates total data without date grouping
    """
    if df.empty:
        return pd.DataFrame()
    
    print(f"[{timeframe_label}] Building {source_type} rollup with SKU attribution")
    print(f"[{timeframe_label}] Input DataFrame shape: {df.shape}")
    
    # Transform the data
    df = transform_attribution_data(df)
    
    # Create unified funnel columns (same logic as dailyrollup.py)
    df = _create_unified_funnel_columns(df, source_type)
    
    if source_type.lower() == 'organic':
        # For Organic: aggregate total data without date/campaign grouping
        return build_organic_total_rollup(df, timeframe_label)
    else:
        # For Meta/Google: aggregate by campaign hierarchy
        return build_meta_google_hierarchy_rollup(df, timeframe_label, source_type)

def build_meta_google_hierarchy_rollup(df: pd.DataFrame, timeframe_label: str, source_type: str) -> pd.DataFrame:
    """
    Build Meta/Google rollup with campaign > adset > ad hierarchy and SKU attribution.
    Aggregates across the entire timeframe without date grouping.
    """
    if df.empty:
        return pd.DataFrame()
    
    # Determine grouping columns based on source type
    if source_type.lower() == 'google ads':
        # Google Ads: campaign level only (no adset/ad)
        group_cols = ['channel', 'campaign_name']
        hierarchy_cols = ['channel', 'campaign_name']
    else:
        # Meta Ads: full hierarchy (campaign > adset > ad)
        group_cols = ['channel', 'campaign_name', 'adset_name', 'ad_name']
        hierarchy_cols = ['channel', 'campaign_name', 'adset_name', 'ad_name']
    
    # Ensure grouping columns exist
    group_cols = [c for c in group_cols if c in df.columns]
    
    if not group_cols:
        print(f"[{timeframe_label}] No grouping columns found for {source_type}")
        return pd.DataFrame()
    
    print(f"[{timeframe_label}] {source_type} grouping by: {group_cols}")
    
    # Ensure numeric columns
    numeric_cols = [
        'impressions', 'clicks', 'spend', 'shopify_orders', 'shopify_revenue', 'shopify_cogs',
        '_landing_page_view', '_add_to_cart', '_initiate_checkout'
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
    
    # Aggregate metrics by hierarchy
    metrics_agg = df.groupby(group_cols, dropna=False)[numeric_cols].sum().reset_index()
    
    # Calculate derived metrics
    with np.errstate(divide='ignore', invalid='ignore'):
        # CTR: Take average from database CTR column
        if 'ctr' in df.columns:
            ctr_avg = df.groupby(group_cols, dropna=False)['ctr'].mean().reset_index()
            # For Google Ads: CTR is stored as decimal (0-1), multiply by 100 for percentage
            # For Meta Ads: CTR is already in percentage format (0-100)
            if source_type.lower() == 'google ads':
                ctr_avg['ctr'] = ctr_avg['ctr'] * 100  # Convert to percentage
            metrics_agg = metrics_agg.merge(ctr_avg[group_cols + ['ctr']], on=group_cols, how='left')
            metrics_agg['ctr'] = metrics_agg['ctr'].fillna(0)
        
        # Bounce Rate: (clicks - landing_page_views) / clicks * 100
        if {'clicks', '_landing_page_view'}.issubset(metrics_agg.columns):
            bounce = ((pd.to_numeric(metrics_agg['clicks'], errors='coerce') - pd.to_numeric(metrics_agg['_landing_page_view'], errors='coerce')) / 
                     pd.to_numeric(metrics_agg['clicks'], errors='coerce') * 100)
            metrics_agg['bounce_rate'] = pd.to_numeric(bounce, errors='coerce').replace([np.inf, -np.inf], 0).fillna(0).clip(lower=0, upper=100)
        
        # Gross ROAS calculation (removed for organic campaigns)
        
        # Net ROAS: (revenue - cogs) / spend
        if {'shopify_revenue', 'shopify_cogs', 'spend'}.issubset(metrics_agg.columns):
            n = (pd.to_numeric(metrics_agg['shopify_revenue'], errors='coerce') - pd.to_numeric(metrics_agg['shopify_cogs'], errors='coerce')) / pd.to_numeric(metrics_agg['spend'], errors='coerce')
            metrics_agg['net_roas'] = pd.to_numeric(n, errors='coerce').replace([np.inf, -np.inf], 0).fillna(0)
            
            # Breakeven ROAS: (cogs + spend) / spend
            b = (pd.to_numeric(metrics_agg['shopify_cogs'], errors='coerce') + pd.to_numeric(metrics_agg['spend'], errors='coerce')) / pd.to_numeric(metrics_agg['spend'], errors='coerce')
            metrics_agg['be_roas'] = pd.to_numeric(b, errors='coerce').replace([np.inf, -np.inf], 0).fillna(0)
            
            # Net profit: revenue - cogs - spend
            metrics_agg['net_profit'] = (pd.to_numeric(metrics_agg['shopify_revenue'], errors='coerce') - 
                                       pd.to_numeric(metrics_agg['shopify_cogs'], errors='coerce') - 
                                       pd.to_numeric(metrics_agg['spend'], errors='coerce')).fillna(0)
        
        # Conversion rate: orders / clicks * 100
        if {'shopify_orders', 'clicks'}.issubset(metrics_agg.columns):
            cr = (pd.to_numeric(metrics_agg['shopify_orders'], errors='coerce') / pd.to_numeric(metrics_agg['clicks'], errors='coerce')) * 100
            metrics_agg['conversion_rate'] = pd.to_numeric(cr, errors='coerce').replace([np.inf, -np.inf], 0).fillna(0)
    
    # Build SKU rollup aggregated by the same hierarchy
    print(f"[{timeframe_label}] Building SKU rollup for {len(df)} input rows")
    sku_rollup = build_hierarchy_sku_rollup(df, group_cols, timeframe_label)
    print(f"[{timeframe_label}] SKU rollup result: {len(sku_rollup)} rows")
    
    if sku_rollup.empty:
        print(f"[{timeframe_label}] No SKU data found, returning metrics only")
        # Add empty SKU columns (same logic as dailyrollup.py)
        for col in ['sku', 'vendor', 'product_title', 'variant_title', 'sku_quantity', 'sku_unit_price', 'sku_unit_cogs']:
            metrics_agg[col] = ''
        
        # Round for output
        metrics_agg = round_for_output(metrics_agg)
        return metrics_agg
    
    # Merge metrics with SKU data (same logic as dailyrollup.py)
    print(f"[{timeframe_label}] Merging metrics ({len(metrics_agg)} rows) with SKU rollup ({len(sku_rollup)} rows)")
    print(f"[{timeframe_label}] Merge keys: {group_cols}")
    
    merged = metrics_agg.merge(
        sku_rollup,
        on=group_cols,
        how='left'
    )
    print(f"[{timeframe_label}] After merge: {len(merged)} rows")
    
    # Fill missing SKU data
    merged['sku'] = merged['sku'].fillna('')
    merged['vendor'] = merged['vendor'].fillna('Unknown')
    merged['product_title'] = merged['product_title'].fillna('Unknown Product')
    merged['variant_title'] = merged['variant_title'].fillna('Unknown Variant')
    for col in ['sku_quantity', 'unit_price', 'unit_cost', 'sku_revenue', 'sku_cogs']:
        if col in merged.columns:
            merged[col] = merged[col].fillna(0)
    
    # Rename unit columns to sku_unit_price and sku_unit_cogs (after merge, before grouping)
    if 'unit_price' in merged.columns:
        merged = merged.rename(columns={'unit_price': 'sku_unit_price'})
    if 'unit_cost' in merged.columns:
        merged = merged.rename(columns={'unit_cost': 'sku_unit_cogs'})
    
    # Handle duplicates by aggregating SKU metrics (same logic as dailyrollup.py)
    group_keys = group_cols + (['sku'] if 'sku' in merged.columns else [])
    group_keys = [k for k in group_keys if k in merged.columns]
    
    if group_keys and len(merged) > 1:
        print(f"[{timeframe_label}] Handling duplicates - grouping by: {group_keys}")
        
        # Metrics to carry over (identical per campaign) – take first to avoid double counting
        campaign_metrics_cols = [
            'impressions', 'clicks', 'spend', 'shopify_orders', 'shopify_revenue', 'shopify_cogs',
            'ctr', 'bounce_rate', 'net_roas', 'be_roas', 'conversion_rate', 'net_profit'
        ]
        
        # SKU numeric fields to sum when duplicates present (only quantity, not unit prices)
        sku_metrics_cols = ['sku_quantity']
        
        # Non-key descriptors (unit prices are per-SKU values, not aggregated)
        descriptor_cols = ['vendor', 'product_title', 'variant_title', 'sku_unit_price', 'sku_unit_cogs']
        
        # Build aggregation dictionary (same pattern as dailyrollup.py)
        present_campaign_metrics = {c: 'first' for c in campaign_metrics_cols if c in merged.columns}
        present_sku_metrics = {c: 'sum' for c in sku_metrics_cols if c in merged.columns}
        present_descriptors = {c: 'first' for c in descriptor_cols if c in merged.columns and c not in group_keys}
        
        agg_dict = {**present_campaign_metrics, **present_sku_metrics, **present_descriptors}
        
        # Carry-over columns not explicitly aggregated -> first (same as dailyrollup.py)
        for col in merged.columns:
            if col in group_keys or col in agg_dict:
                continue
            agg_dict[col] = 'first'
        
        print(f"[{timeframe_label}] Aggregation map: {agg_dict}")
        merged = merged.groupby(group_keys, dropna=False).agg(agg_dict).reset_index()
        print(f"[{timeframe_label}] After grouping: {len(merged)} rows")
        
        # Debug: Show sample SKU values after grouping
        if 'sku' in merged.columns:
            sku_samples = merged['sku'].head(10).tolist()
            print(f"[{timeframe_label}] Sample SKU values after grouping: {sku_samples}")
        
        # Unit prices (sku_unit_price, sku_unit_cogs) are already per-SKU values, no recalculation needed
        
        # Recompute derived metrics from sums (keep original CTR from metrics)
        if 'clicks' in merged.columns and '_landing_page_view' in merged.columns:
            denom = merged['clicks'].replace(0, pd.NA)
            merged['bounce_rate'] = ((merged['clicks'] - merged['_landing_page_view']) / denom * 100).fillna(0).clip(lower=0, upper=100)
        elif 'clicks' in merged.columns:
            # If no landing page view data, set bounce rate to 0
            merged['bounce_rate'] = 0
        
        # Gross ROAS calculation removed for organic campaigns
        
        if {'shopify_revenue', 'shopify_cogs', 'spend'}.issubset(merged.columns):
            # Compute Net ROAS at campaign level
            with np.errstate(divide='ignore', invalid='ignore'):
                net = (merged['shopify_revenue'] - merged['shopify_cogs']) / merged['spend']
            merged['net_roas'] = pd.to_numeric(net, errors='coerce').replace([np.inf, -np.inf], 0).fillna(0)
            
            # Compute Breakeven ROAS
            be = (merged['shopify_cogs'] + merged['spend']) / merged['spend']
            merged['be_roas'] = pd.to_numeric(be, errors='coerce').replace([np.inf, -np.inf], 0).fillna(0)
            
            # Compute Net Profit - use SKU-level revenue/COGS if available, otherwise use ad-level
            # Distribute ad spend proportionally based on SKU revenue contribution
            if 'sku_revenue' in merged.columns and 'sku_cogs' in merged.columns:
                # Calculate SKU-level net profit using SKU revenue/COGS and proportional spend
                # Ensure numeric types
                sku_rev = pd.to_numeric(merged['sku_revenue'], errors='coerce').fillna(0)
                sku_cogs = pd.to_numeric(merged['sku_cogs'], errors='coerce').fillna(0)
                ad_rev = pd.to_numeric(merged['shopify_revenue'], errors='coerce').fillna(0)
                ad_spend = pd.to_numeric(merged['spend'], errors='coerce').fillna(0)
                
                # Calculate proportional spend for each SKU based on revenue contribution
                # If ad revenue is 0, distribute spend equally among SKUs
                with np.errstate(divide='ignore', invalid='ignore'):
                    # Calculate revenue ratio for each SKU
                    revenue_ratio = sku_rev / ad_rev.replace(0, pd.NA)
                    
                    # For rows where ad_rev is 0 or ratio is invalid, distribute spend equally
                    # Count SKUs per ad group (using group_cols which is in scope)
                    if len(group_cols) > 0 and all(col in merged.columns for col in group_cols):
                        # Group by ad-level keys to count SKUs per ad
                        sku_counts = merged.groupby(group_cols, dropna=False).size()
                        sku_counts_dict = sku_counts.to_dict()
                        # Create a key for each row based on group_cols
                        row_keys = merged[group_cols].apply(lambda x: tuple(x), axis=1)
                        equal_ratio = row_keys.map(lambda k: 1.0 / sku_counts_dict.get(k, 1))
                        # Use revenue ratio if valid, otherwise equal distribution
                        spend_ratio = revenue_ratio.fillna(equal_ratio)
                    else:
                        # If no group_cols available, use revenue ratio or 1.0 as fallback
                        spend_ratio = revenue_ratio.fillna(1.0)
                
                # Calculate SKU-level net profit
                sku_spend = ad_spend * spend_ratio.fillna(0)
                merged['net_profit'] = (sku_rev - sku_cogs - sku_spend).fillna(0)
            else:
                # Fallback to ad-level calculation if SKU columns not available
                merged['net_profit'] = (merged['shopify_revenue'] - merged['shopify_cogs'] - merged['spend']).fillna(0)
        
        if 'shopify_orders' in merged.columns and 'clicks' in merged.columns:
            denom = merged['clicks'].replace(0, pd.NA)
            merged['conversion_rate'] = (merged['shopify_orders'] / denom * 100).fillna(0)
    else:
        # If no duplicates, still need to calculate SKU-level net_profit if SKU columns are available
        if {'shopify_revenue', 'shopify_cogs', 'spend'}.issubset(merged.columns):
            if 'sku_revenue' in merged.columns and 'sku_cogs' in merged.columns:
                # Calculate SKU-level net profit using SKU revenue/COGS and proportional spend
                sku_rev = pd.to_numeric(merged['sku_revenue'], errors='coerce').fillna(0)
                sku_cogs = pd.to_numeric(merged['sku_cogs'], errors='coerce').fillna(0)
                ad_rev = pd.to_numeric(merged['shopify_revenue'], errors='coerce').fillna(0)
                ad_spend = pd.to_numeric(merged['spend'], errors='coerce').fillna(0)
                
                # Calculate proportional spend for each SKU based on revenue contribution
                with np.errstate(divide='ignore', invalid='ignore'):
                    revenue_ratio = sku_rev / ad_rev.replace(0, pd.NA)
                    if len(group_cols) > 0 and all(col in merged.columns for col in group_cols):
                        sku_counts = merged.groupby(group_cols, dropna=False).size()
                        sku_counts_dict = sku_counts.to_dict()
                        row_keys = merged[group_cols].apply(lambda x: tuple(x), axis=1)
                        equal_ratio = row_keys.map(lambda k: 1.0 / sku_counts_dict.get(k, 1))
                        spend_ratio = revenue_ratio.fillna(equal_ratio)
                    else:
                        spend_ratio = revenue_ratio.fillna(1.0)
                
                sku_spend = ad_spend * spend_ratio.fillna(0)
                merged['net_profit'] = (sku_rev - sku_cogs - sku_spend).fillna(0)
            else:
                # Fallback to ad-level calculation
                merged['net_profit'] = (merged['shopify_revenue'] - merged['shopify_cogs'] - merged['spend']).fillna(0)
    
    # Round for output
    merged = round_for_output(merged)
    
    # Sort by net_roas descending, then by hierarchy
    sort_cols = []
    if 'net_roas' in merged.columns:
        sort_cols.append('net_roas')
    sort_cols.extend(hierarchy_cols + ['sku'])
    sort_cols = [c for c in sort_cols if c in merged.columns]
    
    if sort_cols:
        ascending_flags = [False] + [True] * (len(sort_cols) - 1)
        merged = merged.sort_values(sort_cols, ascending=ascending_flags, na_position='last').reset_index(drop=True)
    
    print(f"[{timeframe_label}] Generated {len(merged)} {source_type} hierarchy-SKU combinations")
    
    return merged

def build_hierarchy_sku_rollup(df: pd.DataFrame, group_cols: list, timeframe_label: str) -> pd.DataFrame:
    """
    Build SKU rollup aggregated by campaign hierarchy (without date grouping).
    
    Data source handling for WTD/MTD reports:
    - Meta Ads: Uses product_details column directly (exact SKU-level revenue/COGS)
    - Google Ads: Uses product_details column directly (exact SKU-level revenue/COGS)
    - Organic: Uses product_details column directly (exact SKU-level revenue/COGS)
    
    All channels use exact values from product_details, no distribution.
    """
    if df.empty or 'product_details' not in df.columns:
        return pd.DataFrame()
    
    # Determine data source
    data_source = None
    if not df.empty and 'source' in df.columns:
        data_source = df['source'].iloc[0] if len(df) > 0 else None
    
    sku_rows = []
    total_skus_parsed = 0
    
    for idx, row in df.iterrows():
        product_details = row.get('product_details')
        source = row.get('source', 'Unknown')
        
        # For WTD/MTD: ALL channels use product_details directly (exact SKU values)
        # Pass 0 for all attribution parameters to trigger Direct Mode
        attributed_revenue = 0.0
        attributed_cogs = 0.0
        attributed_quantity = 0
        
        print(f"[{timeframe_label}] {source}: Using product_details column directly (exact SKU values)")
        
        # Parse product_details in Direct Mode (exact values from JSONB)
        skus = parse_product_details(
            product_details,
            attributed_revenue=attributed_revenue,
            attributed_cogs=attributed_cogs,
            attributed_quantity=attributed_quantity
        )
        
        if not skus:
            continue
        
        total_skus_parsed += len(skus)
        
        # Create base record with hierarchy columns
        base = {c: row.get(c) for c in group_cols if c in row}
        
        for sku in skus:
            rec = {**base, **sku}
            sku_rows.append(rec)
    
    print(f"[{timeframe_label}] Parsed {total_skus_parsed} SKUs from {len(df)} rows ({data_source})")
    
    if not sku_rows:
        return pd.DataFrame()
    
    sku_df = pd.DataFrame(sku_rows)
    
    # Ensure numeric columns
    for col in ['quantity', 'sku_cogs', 'unit_cost', 'unit_price', 'sku_revenue']:
        if col in sku_df.columns:
            sku_df[col] = pd.to_numeric(sku_df[col], errors='coerce').fillna(0)
    
    # Group by hierarchy + SKU and aggregate (same logic as dailyrollup.py)
    sku_group_cols = group_cols + ['sku']
    sku_group_cols = [c for c in sku_group_cols if c in sku_df.columns]
    
    # Ensure all grouping columns are strings to avoid unhashable type errors
    for col in sku_group_cols:
        if col in sku_df.columns:
            sku_df[col] = sku_df[col].astype(str)
    
    print(f"[{timeframe_label}] SKU rollup grouping by: {sku_group_cols}")
    print(f"[{timeframe_label}] SKU DataFrame shape before grouping: {sku_df.shape}")
    
    # Build aggregation dictionary (same pattern as dailyrollup.py)
    agg_dict = {
        'quantity': 'sum',
        'sku_revenue': 'sum', 
        'sku_cogs': 'sum',
        'vendor': 'first',
        'product_title': 'first',
        'variant_title': 'first',
    }
    present_agg = {k: v for k, v in agg_dict.items() if k in sku_df.columns}
    
    # Carry-over columns not explicitly aggregated -> first (same as dailyrollup.py)
    for col in sku_df.columns:
        if col in sku_group_cols or col in present_agg:
            continue
        present_agg[col] = 'first'
    
    print(f"[{timeframe_label}] SKU aggregation map: {present_agg}")
    rollup = sku_df.groupby(sku_group_cols, dropna=False).agg(present_agg).reset_index()
    print(f"[{timeframe_label}] SKU rollup shape after grouping: {rollup.shape}")
    
    # Debug: Show sample SKU values after grouping
    if 'sku' in rollup.columns:
        sku_samples = rollup['sku'].head(10).tolist()
        print(f"[{timeframe_label}] Sample SKU values in rollup: {sku_samples}")
    
    # Rename quantity to sku_quantity for consistency
    if 'quantity' in rollup.columns:
        rollup = rollup.rename(columns={'quantity': 'sku_quantity'})
    
    # Derive unit price/cost from totals where quantity > 0
    if 'sku_quantity' in rollup.columns:
        qty = rollup['sku_quantity'].replace(0, pd.NA)
        if 'sku_revenue' in rollup.columns:
            rollup['unit_price'] = (rollup['sku_revenue'] / qty).fillna(0)
        if 'sku_cogs' in rollup.columns:
            rollup['unit_cost'] = (rollup['sku_cogs'] / qty).fillna(0)
    
    # Rename unit columns to sku_unit_price and sku_unit_cogs for consistency
    if 'unit_price' in rollup.columns:
        rollup = rollup.rename(columns={'unit_price': 'sku_unit_price'})
    if 'unit_cost' in rollup.columns:
        rollup = rollup.rename(columns={'unit_cost': 'sku_unit_cogs'})
    
    return rollup

def build_organic_total_rollup(df: pd.DataFrame, timeframe_label: str) -> pd.DataFrame:
    """
    Build Organic rollup with total aggregated data (no date/campaign grouping).
    """
    if df.empty:
        return pd.DataFrame()
    
    print(f"[{timeframe_label}] Building Organic total rollup")
    
    # Ensure numeric columns
    numeric_cols = [
        'impressions', 'clicks', 'spend', 'shopify_orders', 'shopify_revenue', 'shopify_cogs',
        '_landing_page_view', '_add_to_cart', '_initiate_checkout'
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
    
    # Calculate total metrics
    total_metrics = {}
    for col in numeric_cols:
        if col in df.columns:
            total_metrics[col] = df[col].sum()
    
    # Calculate derived metrics
    with np.errstate(divide='ignore', invalid='ignore'):
        # CTR
        if 'clicks' in total_metrics and 'impressions' in total_metrics and total_metrics['impressions'] > 0:
            total_metrics['ctr'] = (total_metrics['clicks'] / total_metrics['impressions']) * 100
        else:
            total_metrics['ctr'] = 0.0
        
        # Bounce Rate
        if 'clicks' in total_metrics and '_landing_page_view' in total_metrics and total_metrics['clicks'] > 0:
            bounce = ((total_metrics['clicks'] - total_metrics['_landing_page_view']) / total_metrics['clicks']) * 100
            total_metrics['bounce_rate'] = max(0, min(100, bounce))
        else:
            total_metrics['bounce_rate'] = 0.0
        
        # Net ROAS and Net Profit (gross_roas removed from organic campaigns)
        if 'shopify_revenue' in total_metrics and 'shopify_cogs' in total_metrics and 'spend' in total_metrics:
            # Calculate net profit (even if spend is 0, for organic campaigns)
            total_metrics['net_profit'] = total_metrics['shopify_revenue'] - total_metrics['shopify_cogs'] - total_metrics['spend']
            if total_metrics['spend'] > 0:
                total_metrics['net_roas'] = (total_metrics['shopify_revenue'] - total_metrics['shopify_cogs']) / total_metrics['spend']
                total_metrics['be_roas'] = (total_metrics['shopify_cogs'] + total_metrics['spend']) / total_metrics['spend']
            else:
                total_metrics['net_roas'] = 0.0
                total_metrics['be_roas'] = 0.0
        else:
            total_metrics['net_roas'] = 0.0
            total_metrics['be_roas'] = 0.0
            total_metrics['net_profit'] = 0.0
        
        # Conversion rate
        if 'shopify_orders' in total_metrics and 'clicks' in total_metrics and total_metrics['clicks'] > 0:
            total_metrics['conversion_rate'] = (total_metrics['shopify_orders'] / total_metrics['clicks']) * 100
        else:
            total_metrics['conversion_rate'] = 0.0
    
    # Build SKU rollup for organic
    sku_rollup = build_hierarchy_sku_rollup(df, ['channel'], timeframe_label)
    
    if sku_rollup.empty:
        print(f"[{timeframe_label}] No SKU data found for Organic")
        # Create single row with total metrics
        total_row = {
            'channel': 'Organic',
            'campaign_name': 'Total',
            'adset_name': '',
            'ad_name': '',
            **total_metrics,
            'sku': '',
            'vendor': '',
            'product_title': '',
            'variant_title': '',
            'sku_quantity': 0,
            'sku_unit_price': 0,
            'sku_unit_cogs': 0
        }
        return pd.DataFrame([total_row])
    
    # Add total metrics to each SKU row
    for col, value in total_metrics.items():
        sku_rollup[col] = value
    
    # Add hierarchy columns
    sku_rollup['campaign_name'] = 'Total'
    sku_rollup['adset_name'] = ''
    sku_rollup['ad_name'] = ''
    
    # Round for output
    sku_rollup = round_for_output(sku_rollup)
    
    print(f"[{timeframe_label}] Generated {len(sku_rollup)} Organic SKU combinations")
    
    return sku_rollup


def build_channel_rollups(df: pd.DataFrame, timeframe_label: str) -> dict:
    """
    Build rollups for each channel (Meta, Google, Organic) with SKU attribution.
    Returns dict with channel-specific DataFrames.
    """
    rollups = {}
    
    # Get unique sources/channels
    if 'source' in df.columns:
        sources = df['source'].unique()
    else:
        sources = ['Meta Ads', 'Google Ads', 'Organic']  # Default sources
    
    for source in sources:
        print(f"[{timeframe_label}] Processing {source} data")
        
        # Filter data for this source
        source_df = df[df['source'] == source].copy() if 'source' in df.columns else df.copy()
        
        if source_df.empty:
            print(f"[{timeframe_label}] No {source} data found")
            rollups[source.lower().replace(' ', '_')] = pd.DataFrame()
            continue
        
        # Build campaign rollup with SKU for this source
        source_rollup = build_campaign_rollup_with_sku(source_df, f"{timeframe_label}-{source}", source)
        
        if not source_rollup.empty:
            print(f"[{timeframe_label}] {source} rollup: {len(source_rollup)} rows")
            # Show sample data
            if 'campaign_name' in source_rollup.columns:
                campaigns = source_rollup['campaign_name'].unique()
                print(f"[{timeframe_label}] {source} campaigns: {campaigns[:5]}{'...' if len(campaigns) > 5 else ''}")
        else:
            print(f"[{timeframe_label}] {source} rollup is empty")
        
        rollups[source.lower().replace(' ', '_')] = source_rollup
    
    return rollups

def extract_channel_summary(df: pd.DataFrame, channel_key: str) -> dict:
    """
    Extract summary metrics from channel data for email reporting.
    
    Args:
        df: Channel DataFrame with campaign data
        channel_key: Channel identifier (meta_ads, google_ads, organic)
    
    Returns:
        dict: Summary metrics including revenue, COGS, spend, orders, quantity, and campaign performers
    """
    if df.empty:
        return {
            'revenue': 0.0,
            'cogs': 0.0,
            'spend': 0.0,
            'orders': 0,
            'quantity': 0,
            'net_roas': 0.0,
            'net_profit': 0.0,
            'cost_per_order': 0.0,
            'cost_per_unit': 0.0,
            'avg_order_value': 0.0,
            'top_campaigns': [],
            'bottom_campaigns': []
        }
    
    # Deduplicate by campaign hierarchy to avoid double counting campaign metrics
    campaign_keys = ['channel', 'campaign_name', 'adset_name', 'ad_name']
    present_campaign_keys = [c for c in campaign_keys if c in df.columns]
    
    if present_campaign_keys:
        dedup_campaigns = df.drop_duplicates(subset=present_campaign_keys)
    else:
        dedup_campaigns = df
    
    # Sum campaign-level metrics from deduplicated data
    revenue = float(dedup_campaigns['shopify_revenue'].sum()) if 'shopify_revenue' in dedup_campaigns.columns else 0.0
    cogs = float(dedup_campaigns['shopify_cogs'].sum()) if 'shopify_cogs' in dedup_campaigns.columns else 0.0
    spend = float(dedup_campaigns['spend'].sum()) if 'spend' in dedup_campaigns.columns else 0.0
    orders = int(dedup_campaigns['shopify_orders'].sum()) if 'shopify_orders' in dedup_campaigns.columns else 0
    
    # Sum SKU-level metrics from all rows (including duplicates for SKU quantities)
    quantity = int(df['sku_quantity'].sum()) if 'sku_quantity' in df.columns else 0
    
    # Calculate derived metrics
    net_roas = (revenue - cogs) / spend if spend > 0 else 0.0
    net_profit = revenue - cogs - spend
    
    # Calculate efficiency metrics
    cost_per_order = spend / orders if orders > 0 else 0.0
    cost_per_unit = spend / quantity if quantity > 0 else 0.0
    avg_order_value = revenue / orders if orders > 0 else 0.0
    
    # Extract top and bottom performers by campaign (only for Meta Ads and Google Ads)
    top_campaigns = []
    bottom_campaigns = []
    
    if channel_key in ['meta_ads', 'google_ads'] and 'campaign_name' in dedup_campaigns.columns:
        # Aggregate by campaign name
        campaign_agg = dedup_campaigns.groupby('campaign_name').agg({
            'shopify_revenue': 'sum',
            'spend': 'sum',
            'shopify_cogs': 'sum',
            'shopify_orders': 'sum'
        }).reset_index()
        
        # Calculate net profit for each campaign
        campaign_agg['net_profit'] = campaign_agg['shopify_revenue'] - campaign_agg['shopify_cogs'] - campaign_agg['spend']
        campaign_agg['net_roas'] = (campaign_agg['shopify_revenue'] - campaign_agg['shopify_cogs']) / campaign_agg['spend']
        campaign_agg['net_roas'] = campaign_agg['net_roas'].replace([float('inf'), float('-inf')], 0).fillna(0)
        
        # Filter out Grand Total rows
        campaign_agg = campaign_agg[campaign_agg['campaign_name'] != 'Grand Total']
        
        if not campaign_agg.empty:
            # Sort by revenue (primary) and net profit (secondary) - both descending
            campaign_agg_sorted = campaign_agg.sort_values(['shopify_revenue', 'net_profit'], ascending=[False, False])
            
            # Get top 3 campaigns
            top_3 = campaign_agg_sorted.head(3)
            for _, row in top_3.iterrows():
                top_campaigns.append({
                    'name': row['campaign_name'],
                    'revenue': float(row['shopify_revenue']),
                    'spend': float(row['spend']),
                    'net_profit': float(row['net_profit']),
                    'net_roas': float(row['net_roas']),
                    'orders': int(row['shopify_orders'])
                })
            
            # Get bottom 3 campaigns (only if more than 3 campaigns exist)
            if len(campaign_agg_sorted) > 3:
                bottom_3 = campaign_agg_sorted.tail(3)
                for _, row in bottom_3.iterrows():
                    bottom_campaigns.append({
                        'name': row['campaign_name'],
                        'revenue': float(row['shopify_revenue']),
                        'spend': float(row['spend']),
                        'net_profit': float(row['net_profit']),
                        'net_roas': float(row['net_roas']),
                        'orders': int(row['shopify_orders'])
                    })
    
    return {
        'revenue': revenue,
        'cogs': cogs,
        'spend': spend,
        'orders': orders,
        'quantity': quantity,
        'net_roas': net_roas,
        'net_profit': net_profit,
        'cost_per_order': cost_per_order,
        'cost_per_unit': cost_per_unit,
        'avg_order_value': avg_order_value,
        'top_campaigns': top_campaigns,
        'bottom_campaigns': bottom_campaigns
    }

def add_grand_total_row(df: pd.DataFrame, timeframe_label: str) -> pd.DataFrame:
    """
    Add Grand Total row to the DataFrame with proper aggregation (same logic as dailyrollup.py).
    """
    if df.empty:
        return df
    
    try:
        # Calculate totals from the DataFrame
        numeric_cols = [
            'impressions', 'clicks', 'spend', 'shopify_orders', 'shopify_revenue', 'shopify_cogs',
            'sku_quantity'
        ]
        
        # Sum campaign-level metrics from unique campaigns to avoid double counting (same logic as dailyrollup.py)
        # For WTD/MTD, we group by campaign hierarchy (not date)
        campaign_keys = ['channel', 'campaign_name', 'adset_name', 'ad_name']
        present_campaign_keys = [c for c in campaign_keys if c in df.columns]
        
        if present_campaign_keys:
            dedup_campaigns = df.drop_duplicates(subset=present_campaign_keys)
        else:
            dedup_campaigns = df
        
        print(f"[{timeframe_label}] Grand Total - deduplicated campaigns: {len(dedup_campaigns)} rows from {len(df)} total rows")
        
        # Separate non-SKU and SKU columns (same logic as dailyrollup.py)
        non_sku_cols = [c for c in ['impressions', 'clicks', 'spend', 'shopify_orders', 'shopify_revenue', 'shopify_cogs'] if c in df.columns]
        sku_cols = [c for c in ['sku_quantity'] if c in df.columns]  # Only sum quantity, not unit prices
        
        total_map = {}
        # Use pandas sum() for better precision, then convert to float for final storage (same as dailyrollup.py)
        for col in non_sku_cols:
            total_map[col] = float(dedup_campaigns[col].sum())
        for col in sku_cols:
            total_map[col] = float(df[col].sum())
        
        # Calculate derived totals (same logic as dailyrollup.py)
        with np.errstate(divide='ignore', invalid='ignore'):
            # CTR
            if 'clicks' in total_map and 'impressions' in total_map and total_map['impressions'] > 0:
                total_map['ctr'] = (total_map['clicks'] / total_map['impressions']) * 100
            else:
                total_map['ctr'] = 0.0
            
            # Bounce Rate
            if 'clicks' in total_map and '_landing_page_view' in total_map and total_map['clicks'] > 0:
                bounce = ((total_map['clicks'] - total_map['_landing_page_view']) / total_map['clicks']) * 100
                total_map['bounce_rate'] = max(0, min(100, bounce))
            else:
                total_map['bounce_rate'] = 0.0
            
            # ROAS metrics (gross_roas removed for organic campaigns)
            
            if 'shopify_revenue' in total_map and 'shopify_cogs' in total_map and 'spend' in total_map and total_map['spend'] > 0:
                total_map['net_roas'] = (total_map['shopify_revenue'] - total_map['shopify_cogs']) / total_map['spend']
                total_map['be_roas'] = (total_map['shopify_cogs'] + total_map['spend']) / total_map['spend']
                total_map['net_profit'] = total_map['shopify_revenue'] - total_map['shopify_cogs'] - total_map['spend']
            else:
                total_map['net_roas'] = 0.0
                total_map['be_roas'] = 0.0
                total_map['net_profit'] = 0.0
            
            # Conversion rate
            if 'shopify_orders' in total_map and 'clicks' in total_map and total_map['clicks'] > 0:
                total_map['conversion_rate'] = (total_map['shopify_orders'] / total_map['clicks']) * 100
            else:
                total_map['conversion_rate'] = 0.0
        
        # Build total row
        total_row = {c: '' for c in df.columns}
        if 'channel' in total_row:
            total_row['channel'] = 'All'
        if 'campaign_name' in total_row:
            total_row['campaign_name'] = 'Grand Total'
        if 'adset_name' in total_row:
            total_row['adset_name'] = ''
        if 'ad_name' in total_row:
            total_row['ad_name'] = ''
        if 'sku' in total_row:
            total_row['sku'] = ''
        
        # Copy numeric totals
        for col in numeric_cols + ['ctr', 'bounce_rate', 'net_roas', 'be_roas', 'conversion_rate', 'net_profit']:
            if col in total_row and col in total_map:
                total_row[col] = total_map[col]
        
        # Set unit price and cost to empty for Grand Total (per-unit values don't aggregate)
        if 'sku_unit_price' in total_row:
            total_row['sku_unit_price'] = ''
        if 'sku_unit_cogs' in total_row:
            total_row['sku_unit_cogs'] = ''
        
        # Append total row
        df_with_total = pd.concat([df, pd.DataFrame([total_row])], ignore_index=True)
        
        print(f"[{timeframe_label}] Added Grand Total row")
        return df_with_total
        
    except Exception as e:
        print(f"[{timeframe_label}] Error adding Grand Total row: {e}")
        return df

def run_amazon_report(out_dir: str = None) -> str:
    """
    Generate standalone Amazon campaign report for 2 days earlier.
    Returns path to the generated Excel file.
    Uses get_report_dir() (e.g. /tmp/reports in Azure) when out_dir is None.
    """
    if out_dir is None:
        out_dir = get_report_dir()
    try:
        os.makedirs(out_dir, exist_ok=True)
        
        # Calculate date for 2 days earlier
        now = datetime.now(IST)
        two_days_ago = (now - timedelta(days=2))
        date_str = two_days_ago.strftime('%Y-%m-%d')
        date_display = two_days_ago.strftime('%d-%m-%Y')
        
        print(f"\n[Amazon Report] Generating report for {date_str} (2 days earlier)")
        logger.info(f"Starting Amazon report generation for {date_str}")
        
        # Fetch Amazon data
        amazon_df = fetch_amazon_data(start_date=date_str, end_date=date_str)
        
        if amazon_df.empty:
            print(f"[Amazon Report] No data found for {date_str}")
            logger.warning(f"No Amazon data found for {date_str}")
            return None
        
        print(f"[Amazon Report] Found {len(amazon_df)} records")
        logger.info(f"Found {len(amazon_df)} Amazon records")
        
        # Aggregate by campaign_name
        group_cols = ['campaign_name']
        
        # Ensure numeric columns
        numeric_cols = ['spend', 'orders', 'sales', 'impressions', 'clicks']
        for col in numeric_cols:
            if col in amazon_df.columns:
                amazon_df[col] = pd.to_numeric(amazon_df[col], errors='coerce').fillna(0)
        
        # Aggregate metrics by campaign
        agg_dict = {
            'spend': 'sum',
            'orders': 'sum',
            'sales': 'sum',
            'impressions': 'sum',
            'clicks': 'sum'
        }
        
        campaign_rollup = amazon_df.groupby(group_cols, dropna=False).agg(agg_dict).reset_index()
        
        # Calculate derived metrics
        with np.errstate(divide='ignore', invalid='ignore'):
            # CTR: (clicks / impressions) * 100
            campaign_rollup['ctr'] = (campaign_rollup['clicks'] / campaign_rollup['impressions'] * 100).replace([np.inf, -np.inf], 0).fillna(0)
            
            # ROAS: sales / spend
            campaign_rollup['roas'] = (campaign_rollup['sales'] / campaign_rollup['spend']).replace([np.inf, -np.inf], 0).fillna(0)
        
        # Select only required columns in desired order
        required_cols = ['campaign_name', 'spend', 'orders', 'sales', 'ctr', 'roas']
        campaign_rollup = campaign_rollup[[col for col in required_cols if col in campaign_rollup.columns]]
        
        # Round for output
        campaign_rollup = round_for_output(campaign_rollup)
        
        # Sort by sales descending
        if 'sales' in campaign_rollup.columns:
            campaign_rollup = campaign_rollup.sort_values('sales', ascending=False).reset_index(drop=True)
        
        # Add Grand Total row
        total_row = {}
        total_row['campaign_name'] = 'Grand Total'
        
        for col in ['spend', 'orders', 'sales']:
            if col in campaign_rollup.columns:
                total_row[col] = float(campaign_rollup[col].sum())
        
        # Calculate totals for derived metrics
        if 'spend' in total_row and 'sales' in total_row and total_row['spend'] > 0:
            total_row['roas'] = total_row['sales'] / total_row['spend']
        else:
            total_row['roas'] = 0.0
        
        if 'clicks' in amazon_df.columns and 'impressions' in amazon_df.columns:
            total_clicks = amazon_df['clicks'].sum()
            total_impressions = amazon_df['impressions'].sum()
            total_row['ctr'] = (total_clicks / total_impressions * 100) if total_impressions > 0 else 0.0
        else:
            total_row['ctr'] = 0.0
        
        # Append total row
        campaign_rollup = pd.concat([campaign_rollup, pd.DataFrame([total_row])], ignore_index=True)
        
        print(f"[Amazon Report] Generated {len(campaign_rollup) - 1} campaigns + Grand Total")
        
        # Generate file path
        ts = now.strftime('%Y%m%d_%H%M%S')
        xlsx_path = os.path.join(out_dir, f"Amazon_Campaign_Report_{date_str}_{ts}.xlsx")
        
        # Write to Excel
        print(f"[Amazon Report] Writing to: {xlsx_path}")
        
        with pd.ExcelWriter(
            xlsx_path,
            engine='xlsxwriter',
            engine_kwargs={'options': {'nan_inf_to_errors': True}}
        ) as writer:
            
            sheet_name = f'Amazon Campaigns ({date_display})'
            campaign_rollup.to_excel(writer, sheet_name=sheet_name, index=False)
            
            # Apply formatting
            try:
                workbook = writer.book
                center_fmt = workbook.add_format({'align': 'center', 'valign': 'vcenter'})
                header_fmt = workbook.add_format({
                    'bold': True, 
                    'align': 'center', 
                    'valign': 'vcenter', 
                    'bg_color': '#F2F2F2', 
                    'border': 1
                })
                total_fmt = workbook.add_format({'bold': True, 'bg_color': '#E6F3FF'})
                
                worksheet = writer.sheets[sheet_name]
                worksheet.set_column(0, len(campaign_rollup.columns)-1, None, center_fmt)
                worksheet.freeze_panes(1, 0)
                worksheet.set_row(0, None, header_fmt)
                
                # Color Grand Total row (last row)
                last_row = len(campaign_rollup)
                worksheet.set_row(last_row, None, total_fmt)
                
                # Conditional formatting for ROAS
                if 'roas' in campaign_rollup.columns:
                    roas_col = campaign_rollup.columns.get_loc('roas')
                    green_fmt = workbook.add_format({'font_color': '#006100', 'bg_color': '#C6EFCE'})
                    red_fmt = workbook.add_format({'font_color': '#9C0006', 'bg_color': '#FFC7CE'})
                    worksheet.conditional_format(1, roas_col, len(campaign_rollup), roas_col, {
                        'type': 'cell', 'criteria': '>=', 'value': 1, 'format': green_fmt
                    })
                    worksheet.conditional_format(1, roas_col, len(campaign_rollup), roas_col, {
                        'type': 'cell', 'criteria': '<', 'value': 1, 'format': red_fmt
                    })
                
                # Heatmap for CTR
                if 'ctr' in campaign_rollup.columns:
                    ctr_col = campaign_rollup.columns.get_loc('ctr')
                    worksheet.conditional_format(1, ctr_col, len(campaign_rollup), ctr_col, {
                        'type': '2_color_scale',
                        'min_type': 'num', 'min_value': 0, 'min_color': '#FFFFFF',
                        'max_type': 'num', 'max_value': 5, 'max_color': '#90EE90'
                    })
                
                # Heatmap for Spend
                if 'spend' in campaign_rollup.columns:
                    spend_col = campaign_rollup.columns.get_loc('spend')
                    spend_values = pd.to_numeric(campaign_rollup['spend'], errors='coerce').fillna(0)
                    max_spend = spend_values.max() if len(spend_values) > 0 else 1000
                    worksheet.conditional_format(1, spend_col, len(campaign_rollup), spend_col, {
                        'type': '2_color_scale',
                        'min_type': 'num', 'min_value': 0, 'min_color': '#FFFFFF',
                        'max_type': 'num', 'max_value': max_spend, 'max_color': '#FFFF00'
                    })
                
                # Heatmap for Sales
                if 'sales' in campaign_rollup.columns:
                    sales_col = campaign_rollup.columns.get_loc('sales')
                    sales_values = pd.to_numeric(campaign_rollup['sales'], errors='coerce').fillna(0)
                    max_sales = sales_values.max() if len(sales_values) > 0 else 1000
                    worksheet.conditional_format(1, sales_col, len(campaign_rollup), sales_col, {
                        'type': '2_color_scale',
                        'min_type': 'num', 'min_value': 0, 'min_color': '#FFFFFF',
                        'max_type': 'num', 'max_value': max_sales, 'max_color': '#90EE90'
                    })
                
                print(f"[Amazon Report] Formatting applied successfully")
            except Exception as e:
                print(f"[Amazon Report] Error applying formatting: {e}")
                logger.warning(f"Error applying Amazon formatting: {e}")
        
        print(f"[Amazon Report] Report generated successfully: {xlsx_path}")
        logger.info(f"Amazon report generated: {xlsx_path}")
        return xlsx_path
        
    except Exception as e:
        print(f"[Amazon Report] Error generating report: {e}")
        logger.error(f"Error in Amazon report generation: {e}")
        return None


def run_wtd_mtd_report(out_dir: str = None) -> tuple:
    """
    Generate WTD and MTD Entity reports with campaign-wise aggregation and SKU attribution.
    Returns tuple of (path to Excel file, summary data dict).
    """
    if out_dir is None:
        out_dir = get_report_dir()
    os.makedirs(out_dir, exist_ok=True)
    
    # Get WTD and MTD timeframes
    timeframes = get_wtd_mtd_timeframes()
    
    # Generate timestamp for file naming
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    xlsx_path = os.path.join(out_dir, f"Entity_WTD_MTD_report_{ts}.xlsx")
    
    # Initialize summary data structure (wtd, mtd, daily)
    summary_data = {
        'wtd': {'channels': {}, 'timeframe': None},
        'mtd': {'channels': {}, 'timeframe': None},
        'daily': {'channels': {}, 'timeframe': None}
    }
    
    print(f"[WTD/MTD Report] Generating report: {xlsx_path}")
    
    with pd.ExcelWriter(
        xlsx_path,
        engine='xlsxwriter',
        engine_kwargs={'options': {'nan_inf_to_errors': True}}
    ) as writer:
        
        # Process each timeframe
        for timeframe_key, timeframe_config in timeframes.items():
            start_date = timeframe_config['start_date']
            end_date = timeframe_config['end_date']
            label = timeframe_config['label']
            
            print(f"\n[{label}] Processing timeframe: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
            
            # Store timeframe info in summary
            summary_data[timeframe_key]['timeframe'] = f"{start_date.strftime('%d-%m-%Y')} to {end_date.strftime('%d-%m-%Y')}"
            
            # Format date range for sheet name (DD-MM format, / is invalid in Excel sheet names)
            date_range_str = f"{start_date.strftime('%d-%m')} to {end_date.strftime('%d-%m')}"
            
            # Fetch data for this timeframe
            df = fetch_marketing_hourly(
                start_date.strftime('%Y-%m-%d'),
                end_date.strftime('%Y-%m-%d')
            )
            
            if df.empty:
                print(f"[{label}] No data found for this timeframe")
                # Create empty sheets in order: Meta, Google, Organic
                for channel_suffix in ['meta_ads', 'google_ads', 'organic']:
                    sheet_name = f'{timeframe_key}_{channel_suffix} ({date_range_str})'[:31]
                    pd.DataFrame().to_excel(writer, sheet_name=sheet_name, index=False)
                    summary_data[timeframe_key]['channels'][channel_suffix] = extract_channel_summary(pd.DataFrame(), channel_suffix)
                continue
            
            print(f"[{label}] Found {len(df)} total records")
            print(f"[{label}] Channels available: {df['source'].unique() if 'source' in df.columns else 'No source column'}")
            
            # Build channel rollups
            rollups = build_channel_rollups(df, label)
            
            # Write each channel to separate sheet in specific order: Meta, Google, Organic
            channel_order = ['meta_ads', 'google_ads', 'organic']
            for channel_key in channel_order:
                if channel_key not in rollups:
                    # If channel doesn't exist in rollups, create empty sheet
                    sheet_name = f'{timeframe_key}_{channel_key} ({date_range_str})'[:31]
                    pd.DataFrame().to_excel(writer, sheet_name=sheet_name, index=False)
                    summary_data[timeframe_key]['channels'][channel_key] = extract_channel_summary(pd.DataFrame(), channel_key)
                    continue
                
                channel_df = rollups[channel_key]
                sheet_name = f'{timeframe_key}_{channel_key} ({date_range_str})'[:31]
                
                if not channel_df.empty:
                    print(f"[{label}] Writing {channel_key} data to sheet '{sheet_name}': {len(channel_df)} rows")
                    
                    # Extract summary metrics for this channel (before adding grand total row)
                    channel_summary = extract_channel_summary(channel_df, channel_key)
                    summary_data[timeframe_key]['channels'][channel_key] = channel_summary
                    
                    # Add Grand Total row
                    channel_df_with_total = add_grand_total_row(channel_df, f"{label}-{channel_key}")
                    
                    # Round for output
                    channel_df_rounded = round_for_output(channel_df_with_total)
                    
                    # Apply column ordering (same as dailyrollup.py)
                    channel_df_rounded = channel_df_rounded[order_columns_by_funnel(channel_df_rounded, include_sku=True)]
                    
                    # Rename columns for presentation (same as dailyrollup.py)
                    rename_map = {
                        'shopify_orders': 'orders',
                        'shopify_revenue': 'revenue',
                        'shopify_cogs': 'cogs',
                        'sku_quantity': 'quantity',
                    }
                    channel_df_rounded = channel_df_rounded.rename(columns={k:v for k,v in rename_map.items() if k in channel_df_rounded.columns})
                    
                    # Channel-specific column handling
                    if channel_key == 'organic':
                        # Organic: Only keep specified columns with per-unit pricing
                        organic_keep_cols = ['channel', 'sku', 'sku_unit_price', 'sku_unit_cogs', 'quantity']
                        available_organic_cols = [c for c in organic_keep_cols if c in channel_df_rounded.columns]
                        if available_organic_cols:
                            channel_df_rounded = channel_df_rounded[available_organic_cols]
                    else:
                        # Drop unnecessary columns for cleaner presentation (channel-specific)
                        base_drop_cols = [
                            'vendor', 'product_title', 'variant_title', 'profit_margin', 'total_sku_quantity',
                            'sku_revenue', 'sku_cogs', 'impressions', 'clicks', '_landing_page_view'
                        ]
                        # Channel specific removals
                        if channel_key == 'meta_ads':
                            # Remove be_roas per requirements
                            base_drop_cols.extend([c for c in ['be_roas'] if c in channel_df_rounded.columns])
                        if channel_key == 'google_ads':
                            # Remove bounce_rate, ATC, IC, be_roas per requirements (keep ctr)
                            base_drop_cols.extend([c for c in ['bounce_rate', '_add_to_cart', '_initiate_checkout', 'be_roas'] if c in channel_df_rounded.columns])
                        drop_cols = [c for c in base_drop_cols if c in channel_df_rounded.columns]
                        if drop_cols:
                            channel_df_rounded = channel_df_rounded.drop(columns=drop_cols)
                    
                    # Write to Excel
                    channel_df_rounded.to_excel(writer, sheet_name=sheet_name, index=False)
                    
                    # Apply formatting
                    try:
                        workbook = writer.book
                        center_fmt = workbook.add_format({'align': 'center', 'valign': 'vcenter'})
                        header_fmt = workbook.add_format({'bold': True, 'align': 'center', 'valign': 'vcenter', 'bg_color': '#F2F2F2', 'border': 1})
                        total_fmt = workbook.add_format({'bold': True, 'bg_color': '#E6F3FF'})
                        
                        worksheet = writer.sheets[sheet_name]
                        worksheet.set_column(0, len(channel_df_rounded.columns)-1, None, center_fmt)
                        worksheet.freeze_panes(1, 0)
                        worksheet.set_row(0, None, header_fmt)
                        
                        # Color Grand Total row (last row)
                        try:
                            last_row = len(channel_df_rounded)
                            worksheet.set_row(last_row, None, total_fmt)
                        except Exception:
                            pass
                        
                        # Conditional formatting for profit column
                        if 'net_profit' in channel_df_rounded.columns:
                            profit_col = channel_df_rounded.columns.get_loc('net_profit')
                            green_fmt = workbook.add_format({'font_color': '#006100', 'bg_color': '#C6EFCE'})
                            red_fmt = workbook.add_format({'font_color': '#9C0006', 'bg_color': '#FFC7CE'})
                            worksheet.conditional_format(1, profit_col, len(channel_df_rounded), profit_col, {
                                'type': 'cell', 'criteria': '>', 'value': 0, 'format': green_fmt
                            })
                            worksheet.conditional_format(1, profit_col, len(channel_df_rounded), profit_col, {
                                'type': 'cell', 'criteria': '<', 'value': 0, 'format': red_fmt
                            })
                        
                        # Conditional formatting for Net ROAS < 1
                        if 'net_roas' in channel_df_rounded.columns:
                            net_roas_col = channel_df_rounded.columns.get_loc('net_roas')
                            red_fmt = workbook.add_format({'font_color': '#9C0006', 'bg_color': '#FFC7CE'})
                            worksheet.conditional_format(1, net_roas_col, len(channel_df_rounded), net_roas_col, {
                                'type': 'cell', 'criteria': '<', 'value': 1, 'format': red_fmt
                            })
                        
                        # Heatmap for CTR (skipped automatically for Google if removed)
                        if 'ctr' in channel_df_rounded.columns:
                            ctr_col = channel_df_rounded.columns.get_loc('ctr')
                            worksheet.conditional_format(1, ctr_col, len(channel_df_rounded), ctr_col, {
                                'type': '2_color_scale',
                                'min_type': 'num', 'min_value': 0, 'min_color': '#FFFFFF',
                                'max_type': 'num', 'max_value': 5, 'max_color': '#90EE90'
                            })
                        
                        # Heatmap for Spend
                        if 'spend' in channel_df_rounded.columns:
                            spend_col = channel_df_rounded.columns.get_loc('spend')
                            spend_values = pd.to_numeric(channel_df_rounded['spend'], errors='coerce').fillna(0)
                            max_spend = spend_values.max() if len(spend_values) > 0 else 1000
                            worksheet.conditional_format(1, spend_col, len(channel_df_rounded), spend_col, {
                                'type': '2_color_scale',
                                'min_type': 'num', 'min_value': 0, 'min_color': '#FFFFFF',
                                'max_type': 'num', 'max_value': max_spend, 'max_color': '#FFFF00'
                            })
                        
                        # Merge repeating values for better visual grouping (channel-specific)
                        merge_cols = []
                        # Always include hierarchy columns when present
                        for c in ['channel', 'campaign_name', 'adset_name', 'ad_name']:
                            if c in channel_df_rounded.columns:
                                merge_cols.append(c)
                        # Common campaign-level metrics to merge when present
                        common_merge_metrics = ['spend', 'revenue', 'cogs', 'net_roas', 'net_profit']
                        for c in common_merge_metrics:
                            if c in channel_df_rounded.columns:
                                merge_cols.append(c)
                        if channel_key == 'meta_ads':
                            # Group Bounce rate, ATC, IC, conversion rate; be_roas intentionally excluded
                            for c in ['bounce_rate', '_add_to_cart', '_initiate_checkout', 'conversion_rate', 'orders']:
                                if c in channel_df_rounded.columns:
                                    merge_cols.append(c)
                            # Keep CTR if present (not explicitly required, but harmless)
                            if 'ctr' in channel_df_rounded.columns:
                                merge_cols.append('ctr')
                        elif channel_key == 'google_ads':
                            # Group orders, conversion_rate, and ctr (bounce/ATC/IC/be_roas already dropped)
                            for c in ['orders', 'conversion_rate', 'ctr']:
                                if c in channel_df_rounded.columns:
                                    merge_cols.append(c)
                        
                        _merge_repeating_values_in_sheet(
                            writer,
                            channel_df_rounded,
                            sheet_name,
                            merge_cols,
                            scope_columns=[c for c in ['channel'] if c in channel_df_rounded.columns],
                            sum_columns=[]  # No summing needed since we're showing unit prices per SKU
                        )
                        #
                    except Exception as e:
                        print(f"[{label}] Error applying formatting to {sheet_name}: {e}")
                
                else:
                    print(f"[{label}] No {channel_key} data found, creating empty sheet")
                    pd.DataFrame().to_excel(writer, sheet_name=sheet_name, index=False)
                    # Add empty summary for this channel (if not already added)
                    if channel_key not in summary_data[timeframe_key]['channels']:
                        summary_data[timeframe_key]['channels'][channel_key] = extract_channel_summary(pd.DataFrame(), channel_key)
            
            # Amazon sheets from ClickHouse gold (separate from Meta/Google/Organic).
            # Amazon uses CALENDAR-based windows (Mon-to-today / 1st-to-yesterday),
            # unlike Meta/Google which use the rolling 7/30-day window above.
            print(f"\n[{label}] Processing Amazon data (ClickHouse, calendar window)...")
            logger.info(f"Building ClickHouse Amazon sheets for {label}")
            try:
                amazon_tf = get_amazon_calendar_timeframes().get(timeframe_key)
                if amazon_tf is None:
                    # Fall back to the standard window for any timeframe not
                    # covered by the calendar helper (currently: 'daily').
                    amz_start, amz_end = start_date, end_date
                else:
                    amz_start = amazon_tf['start_date']
                    amz_end = amazon_tf['end_date']
                print(
                    f"[{label}] Amazon calendar range: "
                    f"{amz_start.strftime('%Y-%m-%d')} to {amz_end.strftime('%Y-%m-%d')}"
                )
                # MTD calendar end is already yesterday; WTD/Daily use lag=1
                # so the effective ads window also ends yesterday.
                lag = 0 if timeframe_key == 'mtd' else 1
                add_amazon_sheets_for_timeframe(
                    writer,
                    timeframe_key=timeframe_key,
                    start_date=amz_start,
                    end_date=amz_end,
                    round_for_output_fn=round_for_output,
                    days_lag=lag,
                )
            except Exception as e:
                print(f"[{label}] Failed to add ClickHouse Amazon sheets: {e}")
                logger.warning(f"Failed to add ClickHouse Amazon sheets for {label}: {e}")

    print(f"[WTD/MTD Report] Report generated successfully: {xlsx_path}")
    print(f"[WTD/MTD Report] Summary data: {summary_data}")
    return xlsx_path, summary_data


# Product Profitability sheet column order (dependencies respected)
PP_COLUMN_ORDER = [
    'SKU',
    'Product Title',
    'Ad Spend',
    'Revenue',
    'Quantity',
    'COGS',
    'Net Profit',
    'Contribution',                    # Revenue - COGS (before ads)
    'Contribution Margin %',           # (Revenue - COGS) / Revenue
    'ROAS',                            # Revenue / Ad Spend
    'CAC',                             # Ad Spend / Quantity
    'Ad Spend % of Revenue',           # Ad Spend / Revenue
    'Break-even ROAS',                 # 1 / Contribution Margin %
    'Desired cogs',
    'Actual',                          # Actual Unit COGS
    'Gap',
    'COGS Reduction %',                # Gap / Actual
]
# Columns that should display blank when NaN (ratios/percentages with zero denominator)
PP_BLANK_WHEN_NAN_COLS = [
    'Desired cogs', 'Actual', 'Gap',
    'Contribution Margin %', 'ROAS', 'CAC', 'Ad Spend % of Revenue',
    'Break-even ROAS', 'COGS Reduction %',
]


def _build_product_profitability_df(products: list) -> pd.DataFrame:
    """Build product profitability DataFrame from API products list; includes total row and all derived metrics."""
    if not products:
        return pd.DataFrame(columns=PP_COLUMN_ORDER)
    rows = []
    for p in products:
        spend = float(p.get('spend', 0) or 0)
        revenue = float(p.get('revenue', 0) or 0)
        qty = float(p.get('quantity', 0) or 0)
        cogs = float(p.get('cogs', 0) or 0)
        net_profit = revenue - cogs - spend
        contribution = revenue - cogs

        # Contribution Margin % = (Revenue - COGS) / Revenue
        contrib_margin_pct = (contribution / revenue) if revenue and revenue > 0 else np.nan
        # ROAS = Revenue / Ad Spend
        roas = (revenue / spend) if spend and spend > 0 else np.nan
        # CAC = Ad Spend / Quantity
        cac = (spend / qty) if qty and qty > 0 else np.nan
        # Ad Spend % of Revenue = Ad Spend / Revenue
        ad_spend_pct_rev = (spend / revenue) if revenue and revenue > 0 else np.nan
        # Break-even ROAS = 1 / Contribution Margin %
        be_roas = (1.0 / contrib_margin_pct) if contrib_margin_pct and contrib_margin_pct > 0 else np.nan

        # When ad spend is 0, leave Desired cogs and dependent metrics (Actual, Gap) blank
        if spend == 0:
            desired_cogs = np.nan
            actual = np.nan
            gap = np.nan
        else:
            desired_cogs = ((revenue - spend) / qty) if qty and qty > 0 else np.nan
            actual = (cogs / qty) if qty and qty > 0 else np.nan
            gap = (actual - desired_cogs) if not (np.isnan(actual) or np.isnan(desired_cogs)) else np.nan

        # COGS Reduction % = Gap / Actual (share of unit COGS that must be reduced to hit desired)
        cogs_reduction_pct = (gap / actual) if not np.isnan(actual) and actual and actual != 0 else np.nan
        if spend == 0:
            cogs_reduction_pct = np.nan

        rows.append({
            'SKU': p.get('sku', ''),
            'Product Title': p.get('product_title', ''),
            'Ad Spend': spend,
            'Revenue': revenue,
            'Quantity': qty,
            'COGS': cogs,
            'Net Profit': net_profit,
            'Contribution': contribution,
            'Contribution Margin %': contrib_margin_pct,
            'ROAS': roas,
            'CAC': cac,
            'Ad Spend % of Revenue': ad_spend_pct_rev,
            'Break-even ROAS': be_roas,
            'Desired cogs': desired_cogs,
            'Actual': actual if not np.isnan(actual) else np.nan,
            'Gap': gap if not np.isnan(gap) else np.nan,
            'COGS Reduction %': cogs_reduction_pct,
        })
    pp_df = pd.DataFrame(rows)

    # Total row (Blended metrics)
    total_spend = pp_df['Ad Spend'].sum()
    total_revenue = pp_df['Revenue'].sum()
    total_qty = pp_df['Quantity'].sum()
    total_cogs = pp_df['COGS'].sum()
    total_net = pp_df['Net Profit'].sum()
    total_contrib = pp_df['Contribution'].sum()

    if total_spend == 0:
        total_desired = total_actual = total_gap = np.nan
    else:
        total_desired = pp_df['Desired cogs'].sum()
        total_actual = pp_df['Actual'].sum() if pp_df['Actual'].notna().any() else np.nan
        total_gap = pp_df['Gap'].sum() if pp_df['Gap'].notna().any() else np.nan

    # Blended Contribution Margin % = (Total Revenue - Total COGS) / Total Revenue
    blended_contrib_margin_pct = (total_contrib / total_revenue) if total_revenue and total_revenue > 0 else np.nan
    # Blended ROAS = Total Revenue / Total Ad Spend
    blended_roas = (total_revenue / total_spend) if total_spend and total_spend > 0 else np.nan
    # Blended CAC = Total Ad Spend / Total Quantity (orders/units)
    blended_cac = (total_spend / total_qty) if total_qty and total_qty > 0 else np.nan
    # Ad Spend % of Revenue (total)
    total_ad_spend_pct = (total_spend / total_revenue) if total_revenue and total_revenue > 0 else np.nan
    # Break-even ROAS (total)
    total_be_roas = (1.0 / blended_contrib_margin_pct) if blended_contrib_margin_pct and blended_contrib_margin_pct > 0 else np.nan
    # COGS Reduction % (total): Gap / Actual
    total_cogs_red_pct = (total_gap / total_actual) if not np.isnan(total_actual) and total_actual and total_actual != 0 else np.nan
    if total_spend == 0:
        total_cogs_red_pct = np.nan

    total_row = {
        'SKU': f'{len(pp_df)} Products',
        'Product Title': '',
        'Ad Spend': total_spend,
        'Revenue': total_revenue,
        'Quantity': total_qty,
        'COGS': total_cogs,
        'Net Profit': total_net,
        'Contribution': total_contrib,
        'Contribution Margin %': blended_contrib_margin_pct,
        'ROAS': blended_roas,
        'CAC': blended_cac,
        'Ad Spend % of Revenue': total_ad_spend_pct,
        'Break-even ROAS': total_be_roas,
        'Desired cogs': total_desired,
        'Actual': total_actual,
        'Gap': total_gap,
        'COGS Reduction %': total_cogs_red_pct,
    }
    pp_df = pd.concat([pp_df, pd.DataFrame([total_row])], ignore_index=True)
    pp_df = pp_df[[c for c in PP_COLUMN_ORDER if c in pp_df.columns]]
    pp_df = round_for_output(pp_df)
    # Round to 2 decimal places; percentage columns to 4 so Excel % format shows e.g. 48.05%
    PP_PCT_COLS = ('Contribution Margin %', 'Ad Spend % of Revenue', 'COGS Reduction %')
    for col in pp_df.columns:
        if col in ('SKU', 'Product Title'):
            continue
        s = pd.to_numeric(pp_df[col], errors='coerce')
        pp_df[col] = s.round(4) if col in PP_PCT_COLS else s.round(2)
    for col in PP_BLANK_WHEN_NAN_COLS:
        if col in pp_df.columns:
            pp_df[col] = pp_df[col].apply(lambda x: '' if pd.isna(x) else x)
    return pp_df


def _apply_pp_sheet_formatting(writer, sheet_name: str, pp_df: pd.DataFrame) -> None:
    """Apply standard formatting to a Product Profitability sheet."""
    if pp_df.empty:
        return
    try:
        workbook = writer.book
        header_fmt = workbook.add_format({
            'bold': True, 'align': 'center', 'valign': 'vcenter',
            'bg_color': '#E8F4EA', 'border': 1
        })
        total_fmt = workbook.add_format({'bold': True, 'bg_color': '#E6F3FF', 'border': 1})
        center_fmt = workbook.add_format({'align': 'center', 'valign': 'vcenter'})
        num_fmt = workbook.add_format({'align': 'right', 'valign': 'vcenter', 'num_format': '#,##0.00'})
        pct_fmt = workbook.add_format({'align': 'right', 'valign': 'vcenter', 'num_format': '0.00%'})
        # Right-align but no number format for columns that can be blank (so blank stays blank)
        right_fmt = workbook.add_format({'align': 'right', 'valign': 'vcenter'})
        pct_cols = ('Contribution Margin %', 'Ad Spend % of Revenue', 'COGS Reduction %')
        worksheet = writer.sheets[sheet_name]
        ncols = len(pp_df.columns)
        cols_list = list(pp_df.columns)
        worksheet.set_column(0, 0, 18, center_fmt)
        worksheet.set_column(1, 1, 50, center_fmt)
        for c in range(2, ncols):
            col_name = cols_list[c] if c < len(cols_list) else ''
            if col_name in PP_BLANK_WHEN_NAN_COLS:
                fmt = right_fmt  # blank cells stay blank
            elif col_name in pct_cols:
                fmt = pct_fmt    # display as percentage (0.25 -> 25.00%)
            else:
                fmt = num_fmt
            worksheet.set_column(c, c, 14, fmt)
        worksheet.freeze_panes(1, 0)
        worksheet.set_row(0, None, header_fmt)
        last_row = len(pp_df)
        worksheet.set_row(last_row, None, total_fmt)
        if 'Net Profit' in pp_df.columns:
            profit_col = pp_df.columns.get_loc('Net Profit')
            green_fmt = workbook.add_format({'font_color': '#006100', 'bg_color': '#C6EFCE'})
            red_fmt = workbook.add_format({'font_color': '#9C0006', 'bg_color': '#FFC7CE'})
            worksheet.conditional_format(1, profit_col, last_row - 1, profit_col, {
                'type': 'cell', 'criteria': '>', 'value': 0, 'format': green_fmt
            })
            worksheet.conditional_format(1, profit_col, last_row - 1, profit_col, {
                'type': 'cell', 'criteria': '<', 'value': 0, 'format': red_fmt
            })
        if 'Ad Spend' in pp_df.columns:
            spend_col = pp_df.columns.get_loc('Ad Spend')
            max_spend = pp_df.iloc[:-1]['Ad Spend'].max() if len(pp_df) > 1 else 1000
            worksheet.conditional_format(1, spend_col, last_row - 1, spend_col, {
                'type': '2_color_scale',
                'min_type': 'num', 'min_value': 0, 'min_color': '#FFFFFF',
                'max_type': 'num', 'max_value': max_spend or 1000, 'max_color': '#FFFF99'
            })
        if 'Revenue' in pp_df.columns:
            rev_col = pp_df.columns.get_loc('Revenue')
            max_rev = pp_df.iloc[:-1]['Revenue'].max() if len(pp_df) > 1 else 1000
            worksheet.conditional_format(1, rev_col, last_row - 1, rev_col, {
                'type': '2_color_scale',
                'min_type': 'num', 'min_value': 0, 'min_color': '#FFFFFF',
                'max_type': 'num', 'max_value': max_rev or 1000, 'max_color': '#C6EFCE'
            })
    except Exception as e:
        logger.warning(f"Product Profitability sheet formatting: {e}")


def run_product_profitability_report(for_date: str = None, out_dir: str = None) -> str:
    """
    Generate a separate Product Profitability Excel file with WTD, MTD, and Daily sheets (with date ranges).
    Daily uses previous day. Returns path to the generated Excel file.
    """
    if out_dir is None:
        out_dir = get_report_dir()
    os.makedirs(out_dir, exist_ok=True)
    now = datetime.now(IST)
    ts = now.strftime('%Y%m%d_%H%M%S')
    yesterday_str = (now - timedelta(days=1)).strftime('%Y-%m-%d')
    xlsx_path = os.path.join(out_dir, f"Product_Profitability_{yesterday_str}_{ts}.xlsx")
    timeframes = get_wtd_mtd_timeframes()
    # Order: WTD, MTD, Daily (so Daily is last / most recent single day)
    pp_empty = pd.DataFrame(columns=PP_COLUMN_ORDER)
    print(f"[Product Profitability] Generating report (WTD, MTD, Daily): {xlsx_path}")
    try:
        with pd.ExcelWriter(
            xlsx_path,
            engine='xlsxwriter',
            engine_kwargs={'options': {'nan_inf_to_errors': True}}
        ) as writer:
            for key in ['wtd', 'mtd', 'daily']:
                cfg = timeframes.get(key)
                if not cfg:
                    continue
                start_dt = cfg['start_date']
                end_dt = cfg['end_date']
                start_str = start_dt.strftime('%Y-%m-%d')
                end_str = end_dt.strftime('%Y-%m-%d')
                date_range_str = f"{start_dt.strftime('%d-%m')} to {end_dt.strftime('%d-%m')}"
                if key == 'daily':
                    sheet_name = f'Daily Prod Profit ({date_range_str})'[:31]
                else:
                    sheet_name = f'{key.upper()} Prod Profit ({date_range_str})'[:31]
                print(f"[Product Profitability] Fetching {key.upper()} {start_str} to {end_str}...")
                try:
                    pp_data = fetch_product_profitability(start_str, end_str)
                    products = pp_data.get('products') if isinstance(pp_data, dict) else []
                    pp_df = _build_product_profitability_df(products)
                except Exception as e:
                    logger.warning(f"Product Profitability {key}: {e}")
                    pp_df = pp_empty.copy()
                pp_df.to_excel(writer, sheet_name=sheet_name, index=False)
                _apply_pp_sheet_formatting(writer, sheet_name, pp_df)
        print(f"[Product Profitability] Report written: {xlsx_path}")
    except Exception as e:
        print(f"[Product Profitability] Error: {e}")
        logger.warning(f"Product Profitability report: {e}")
        with pd.ExcelWriter(
            xlsx_path,
            engine='xlsxwriter',
            engine_kwargs={'options': {'nan_inf_to_errors': True}}
        ) as writer:
            for key in ['wtd', 'mtd', 'daily']:
                cfg = timeframes.get(key)
                if not cfg:
                    continue
                start_dt = cfg['start_date']
                end_dt = cfg['end_date']
                date_range_str = f"{start_dt.strftime('%d-%m')} to {end_dt.strftime('%d-%m')}"
                sheet_name = (f'Daily Prod Profit ({date_range_str})' if key == 'daily' else f'{key.upper()} Prod Profit ({date_range_str})')[:31]
                pp_empty.to_excel(writer, sheet_name=sheet_name, index=False)
    return xlsx_path


def extract_daily_efficiency_metrics(daily_file_path: str) -> dict:
    """
    Extract efficiency metrics from daily Excel report.
    
    Args:
        daily_file_path: Path to daily Excel report file
    
    Returns:
        dict: Daily efficiency metrics
    """
    metrics = {
        'revenue': 0.0,
        'spend': 0.0,
        'orders': 0,
        'quantity': 0,
        'cost_per_order': 0.0,
        'cost_per_unit': 0.0,
        'avg_order_value': 0.0
    }
    
    if not daily_file_path or not os.path.exists(daily_file_path):
        logger.warning("Daily file path not provided or file does not exist")
        return metrics
    
    try:
        # Strategy: Read the Grand Total row from each sheet (already calculated correctly)
        total_revenue = 0.0
        total_spend = 0.0
        total_orders = 0
        total_quantity = 0
        
        for sheet_name in ['meta_campaigns', 'google_campaigns']:
            try:
                df = pd.read_excel(daily_file_path, sheet_name=sheet_name)
                logger.info(f"Read {sheet_name} from daily report: {len(df)} rows")
                logger.info(f"Columns in {sheet_name}: {list(df.columns)}")
                
                if not df.empty and 'campaign_name' in df.columns:
                    # Separate data rows and Grand Total row
                    df_data = df[df['campaign_name'] != 'Grand Total']
                    grand_total_row = df[df['campaign_name'] == 'Grand Total']
                    
                    # Extract campaign-level metrics from Grand Total row
                    if not grand_total_row.empty:
                        logger.info(f"{sheet_name} - Found Grand Total row")
                        gt_row = grand_total_row.iloc[-1]  # Take last occurrence
                        
                        sheet_revenue = float(gt_row['revenue']) if 'revenue' in gt_row and pd.notna(gt_row['revenue']) else 0.0
                        sheet_spend = float(gt_row['spend']) if 'spend' in gt_row and pd.notna(gt_row['spend']) else 0.0
                        sheet_orders = int(gt_row['orders']) if 'orders' in gt_row and pd.notna(gt_row['orders']) else 0
                        
                        logger.info(f"{sheet_name} Grand Total - Revenue: ₹{sheet_revenue:.2f}, Spend: ₹{sheet_spend:.2f}, Orders: {sheet_orders}")
                    else:
                        logger.warning(f"{sheet_name} - No Grand Total row found, calculating from data")
                        
                        if not df_data.empty:
                            # Deduplicate by campaign_name to avoid double counting
                            unique_campaigns = df_data.groupby('campaign_name').agg({
                                'revenue': 'first',
                                'spend': 'first',
                                'orders': 'first'
                            }).reset_index()
                            
                            sheet_revenue = unique_campaigns['revenue'].sum() if 'revenue' in unique_campaigns.columns else 0.0
                            sheet_spend = unique_campaigns['spend'].sum() if 'spend' in unique_campaigns.columns else 0.0
                            sheet_orders = unique_campaigns['orders'].sum() if 'orders' in unique_campaigns.columns else 0
                        else:
                            sheet_revenue = 0.0
                            sheet_spend = 0.0
                            sheet_orders = 0
                    
                    # Extract quantity from data rows (Grand Total has empty quantity for unit-level metrics)
                    sheet_quantity = 0
                    if not df_data.empty:
                        # Quantity needs to be summed from all data rows (including SKU rows)
                        if 'quantity' in df_data.columns:
                            sheet_quantity = int(df_data['quantity'].sum())
                            logger.info(f"{sheet_name} - Quantity from data rows: {sheet_quantity}")
                        else:
                            logger.warning(f"{sheet_name} - No 'quantity' column found")
                    
                    total_revenue += sheet_revenue
                    total_spend += sheet_spend
                    total_orders += sheet_orders
                    total_quantity += sheet_quantity
                    
                    logger.info(f"{sheet_name} Totals - Revenue: ₹{sheet_revenue:.2f}, Spend: ₹{sheet_spend:.2f}, Orders: {sheet_orders}, Quantity: {sheet_quantity}")
                    
            except Exception as e:
                logger.warning(f"Could not read {sheet_name} from daily report: {e}")
        
        logger.info(f"Daily totals - Revenue: {total_revenue:.2f}, Spend: {total_spend:.2f}, Orders: {total_orders}, Quantity: {total_quantity}")
        
        # Calculate efficiency metrics
        metrics['revenue'] = total_revenue
        metrics['spend'] = total_spend
        metrics['orders'] = total_orders
        metrics['quantity'] = total_quantity
        metrics['cost_per_order'] = total_spend / total_orders if total_orders > 0 else 0.0
        metrics['cost_per_unit'] = total_spend / total_quantity if total_quantity > 0 else 0.0
        metrics['avg_order_value'] = total_revenue / total_orders if total_orders > 0 else 0.0
        
        logger.info(f"Daily efficiency metrics - CPO: ₹{metrics['cost_per_order']:.2f}, CPU: ₹{metrics['cost_per_unit']:.2f}, AOV: ₹{metrics['avg_order_value']:.2f}")
        
    except Exception as e:
        logger.error(f"Error extracting daily efficiency metrics: {e}")
    
    return metrics

def get_amazon_summary_metrics(start_date: datetime, end_date: datetime) -> dict:
    """
    Get Amazon metrics for a specific date range to include in summary.
    
    Args:
        start_date: Start date (datetime object)
        end_date: End date (datetime object)
    
    Returns:
        dict: Amazon metrics including revenue, spend, orders, and date range
    """
    try:
        start_str = start_date.strftime('%Y-%m-%d')
        end_str = end_date.strftime('%Y-%m-%d')
        start_display = start_date.strftime('%d-%m-%Y')
        end_display = end_date.strftime('%d-%m-%Y')

        if end_date < start_date:
            logger.warning(
                "Amazon summary range invalid (%s > %s); returning empty metrics",
                start_str,
                end_str,
            )
            return {
                'revenue': 0.0,
                'spend': 0.0,
                'orders': 0,
                'start_date': start_display,
                'end_date': end_display,
                'date_range': f"{start_display} to {end_display}",
                'available': False,
            }

        return get_amazon_clickhouse_summary(start_str, end_str)
    except Exception as e:
        logger.error(f"Error fetching Amazon summary metrics: {e}")
        start_display = start_date.strftime('%d-%m-%Y')
        end_display = end_date.strftime('%d-%m-%Y')
        return {
            'revenue': 0.0,
            'spend': 0.0,
            'orders': 0,
            'start_date': start_display,
            'end_date': end_display,
            'date_range': f"{start_display} to {end_display}",
            'available': False
        }


def format_summary_for_email(summary_data: dict, amazon_wtd: dict = None, amazon_mtd: dict = None, daily_metrics: dict = None) -> str:
    """
    Format summary data into HTML tables for email body with efficiency metrics and campaign performers.
    
    Args:
        summary_data: Dictionary containing WTD and MTD summary metrics
        amazon_wtd: Dictionary containing Amazon metrics for WTD date range
        amazon_mtd: Dictionary containing Amazon metrics for MTD date range
        daily_metrics: Dictionary containing daily efficiency metrics (not used in current implementation)
    
    Returns:
        str: HTML formatted summary tables with WTD vs MTD efficiency comparison
    """
    html_parts = []
    
    # Define channel display names
    channel_names = {
        'meta_ads': 'Meta Ads',
        'google_ads': 'Google Ads',
        'organic': 'Organic'
    }
    
    # Calculate efficiency metrics for WTD and MTD
    wtd_data = summary_data.get('wtd', {})
    mtd_data = summary_data.get('mtd', {})
    
    wtd_channels = wtd_data.get('channels', {})
    mtd_channels = mtd_data.get('channels', {})
    
    # WTD totals
    wtd_revenue = sum(ch.get('revenue', 0) for ch in wtd_channels.values())
    wtd_spend = sum(ch.get('spend', 0) for ch in wtd_channels.values())
    wtd_orders = sum(ch.get('orders', 0) for ch in wtd_channels.values())
    wtd_quantity = sum(ch.get('quantity', 0) for ch in wtd_channels.values())
    wtd_cpo = wtd_spend / wtd_orders if wtd_orders > 0 else 0.0
    wtd_cpu = wtd_spend / wtd_quantity if wtd_quantity > 0 else 0.0
    wtd_aov = wtd_revenue / wtd_orders if wtd_orders > 0 else 0.0
    
    # MTD totals
    mtd_revenue = sum(ch.get('revenue', 0) for ch in mtd_channels.values())
    mtd_spend = sum(ch.get('spend', 0) for ch in mtd_channels.values())
    mtd_orders = sum(ch.get('orders', 0) for ch in mtd_channels.values())
    mtd_quantity = sum(ch.get('quantity', 0) for ch in mtd_channels.values())
    mtd_cpo = mtd_spend / mtd_orders if mtd_orders > 0 else 0.0
    mtd_cpu = mtd_spend / mtd_quantity if mtd_quantity > 0 else 0.0
    mtd_aov = mtd_revenue / mtd_orders if mtd_orders > 0 else 0.0
    
    # Add Efficiency Metrics Comparison Table (WTD vs MTD only)
    html_parts.append("""
        <h3 style="color: #333; border-bottom: 1px solid #ddd; padding-bottom: 8px; margin-top: 10px; font-weight: 600;">Performance Efficiency Comparison</h3>
        <p style="color: #666; font-size: 13px; margin-bottom: 15px;">Comparison of key efficiency metrics between WTD and MTD periods.</p>
        
        <table style="border-collapse: collapse; width: 100%; margin-bottom: 25px; border: 1px solid #ddd;">
            <thead>
                <tr style="background-color: #f5f5f5;">
                    <th style="padding: 8px; text-align: left; border: 1px solid #ddd; font-size: 12px; font-weight: 600;">Metric</th>
                    <th style="padding: 8px; text-align: right; border: 1px solid #ddd; font-size: 12px; font-weight: 600;">Week-to-Date (WTD)</th>
                    <th style="padding: 8px; text-align: right; border: 1px solid #ddd; font-size: 12px; font-weight: 600;">Month-to-Date (MTD)</th>
                    <th style="padding: 8px; text-align: center; border: 1px solid #ddd; font-size: 12px; font-weight: 600;">Change</th>
                </tr>
            </thead>
            <tbody>
    """)
    
    # Helper function to calculate percentage change with arrow and color
    def format_wtd_mtd_change(wtd_val, mtd_val, is_cost_metric=True):
        if mtd_val == 0:
            return "N/A", ""
        change = ((wtd_val - mtd_val) / mtd_val) * 100
        
        # Determine arrow and color
        if abs(change) < 0.1:
            return "—", ""  # No significant change
        
        arrow = "↑" if change > 0 else "↓"
        
        # For cost metrics (lower is better), green for decrease, red for increase
        # For revenue metrics (higher is better), green for increase, red for decrease
        if is_cost_metric:
            bg_color = "background-color: #e8f5e9;" if change < 0 else "background-color: #ffebee;"
        else:
            bg_color = "background-color: #e8f5e9;" if change > 0 else "background-color: #ffebee;"
        
        return f"{arrow} {abs(change):.1f}%", bg_color
    
    # Cost Per Order
    cpo_change_text, cpo_bg = format_wtd_mtd_change(wtd_cpo, mtd_cpo, is_cost_metric=True)
    
    html_parts.append(f"""
                <tr>
                    <td style="padding: 7px; border: 1px solid #ddd; font-size: 12px;">Cost Per Order (CPO)</td>
                    <td style="padding: 7px; text-align: right; border: 1px solid #ddd; font-size: 12px;">₹{wtd_cpo:,.2f}</td>
                    <td style="padding: 7px; text-align: right; border: 1px solid #ddd; font-size: 12px;">₹{mtd_cpo:,.2f}</td>
                    <td style="padding: 7px; text-align: center; border: 1px solid #ddd; font-size: 12px; {cpo_bg}">{cpo_change_text}</td>
                </tr>
    """)
    
    # Cost Per Unit
    cpu_change_text, cpu_bg = format_wtd_mtd_change(wtd_cpu, mtd_cpu, is_cost_metric=True)
    
    html_parts.append(f"""
                <tr>
                    <td style="padding: 7px; border: 1px solid #ddd; font-size: 12px;">Cost Per Unit (CPU)</td>
                    <td style="padding: 7px; text-align: right; border: 1px solid #ddd; font-size: 12px;">₹{wtd_cpu:,.2f}</td>
                    <td style="padding: 7px; text-align: right; border: 1px solid #ddd; font-size: 12px;">₹{mtd_cpu:,.2f}</td>
                    <td style="padding: 7px; text-align: center; border: 1px solid #ddd; font-size: 12px; {cpu_bg}">{cpu_change_text}</td>
                </tr>
    """)
    
    # Average Order Value
    aov_change_text, aov_bg = format_wtd_mtd_change(wtd_aov, mtd_aov, is_cost_metric=False)
    
    html_parts.append(f"""
                <tr>
                    <td style="padding: 7px; border: 1px solid #ddd; font-size: 12px;">Average Order Value (AOV)</td>
                    <td style="padding: 7px; text-align: right; border: 1px solid #ddd; font-size: 12px;">₹{wtd_aov:,.2f}</td>
                    <td style="padding: 7px; text-align: right; border: 1px solid #ddd; font-size: 12px;">₹{mtd_aov:,.2f}</td>
                    <td style="padding: 7px; text-align: center; border: 1px solid #ddd; font-size: 12px; {aov_bg}">{aov_change_text}</td>
                </tr>
            </tbody>
        </table>
        <p style="color: #888; font-size: 11px; margin-top: -15px; margin-bottom: 20px;">
            <em>Note: ↑ indicates increase, ↓ indicates decrease. Green highlights better performance, red highlights worse performance.</em>
        </p>
    """)
    
    # Process WTD and MTD separately for channel performance
    for timeframe_key in ['wtd', 'mtd']:
        timeframe_data = summary_data.get(timeframe_key, {})
        timeframe_label = 'Week-to-Date (WTD)' if timeframe_key == 'wtd' else 'Month-to-Date (MTD)'
        timeframe_range = timeframe_data.get('timeframe', 'N/A')
        channels = timeframe_data.get('channels', {})
        
        if not channels:
            continue
        
        # Calculate overall totals across all channels
        total_revenue = sum(ch.get('revenue', 0) for ch in channels.values())
        total_cogs = sum(ch.get('cogs', 0) for ch in channels.values())
        total_spend = sum(ch.get('spend', 0) for ch in channels.values())
        total_orders = sum(ch.get('orders', 0) for ch in channels.values())
        total_quantity = sum(ch.get('quantity', 0) for ch in channels.values())
        total_net_profit = sum(ch.get('net_profit', 0) for ch in channels.values())
        total_net_roas = (total_revenue - total_cogs) / total_spend if total_spend > 0 else 0.0
        
        # Calculate overall efficiency metrics
        total_cost_per_order = total_spend / total_orders if total_orders > 0 else 0.0
        total_cost_per_unit = total_spend / total_quantity if total_quantity > 0 else 0.0
        total_avg_order_value = total_revenue / total_orders if total_orders > 0 else 0.0
        
        html_parts.append(f"""
            <h3 style="color: #333; border-bottom: 1px solid #ddd; padding-bottom: 8px; margin-top: 20px; font-weight: 600;">{timeframe_label}</h3>
            <p style="color: #666; font-size: 13px; margin-bottom: 15px;">Period: {timeframe_range}</p>
            
            <h4 style="color: #555; margin-top: 18px; margin-bottom: 8px; font-weight: 600; font-size: 14px;">Channel Performance</h4>
            <table style="border-collapse: collapse; width: 100%; margin-bottom: 20px; border: 1px solid #ddd;">
                <thead>
                    <tr style="background-color: #f5f5f5;">
                        <th style="padding: 8px; text-align: left; border: 1px solid #ddd; font-size: 12px; font-weight: 600;">Channel</th>
                        <th style="padding: 8px; text-align: right; border: 1px solid #ddd; font-size: 12px; font-weight: 600;">Revenue</th>
                        <th style="padding: 8px; text-align: right; border: 1px solid #ddd; font-size: 12px; font-weight: 600;">COGS</th>
                        <th style="padding: 8px; text-align: right; border: 1px solid #ddd; font-size: 12px; font-weight: 600;">Spend</th>
                        <th style="padding: 8px; text-align: right; border: 1px solid #ddd; font-size: 12px; font-weight: 600;">Net Profit</th>
                        <th style="padding: 8px; text-align: right; border: 1px solid #ddd; font-size: 12px; font-weight: 600;">Net ROAS</th>
                        <th style="padding: 8px; text-align: right; border: 1px solid #ddd; font-size: 12px; font-weight: 600;">Orders</th>
                        <th style="padding: 8px; text-align: right; border: 1px solid #ddd; font-size: 12px; font-weight: 600;">Units</th>
                    </tr>
                </thead>
                <tbody>
        """)
        
        # Add rows for each channel
        for channel_key in ['meta_ads', 'google_ads', 'organic']:
            if channel_key in channels:
                ch_data = channels[channel_key]
                channel_display = channel_names.get(channel_key, channel_key)
                
                # Subtle color hints for net profit and ROAS
                profit = ch_data.get('net_profit', 0)
                profit_bg = "background-color: #e8f5e9;" if profit > 0 else ("background-color: #ffebee;" if profit < 0 else "")
                
                net_roas = ch_data.get('net_roas', 0)
                roas_bg = "background-color: #e8f5e9;" if net_roas >= 1 else "background-color: #ffebee;"
                
                html_parts.append(f"""
                    <tr>
                        <td style="padding: 7px; border: 1px solid #ddd; font-size: 12px;">{channel_display}</td>
                        <td style="padding: 7px; text-align: right; border: 1px solid #ddd; font-size: 12px;">₹{ch_data.get('revenue', 0):,.2f}</td>
                        <td style="padding: 7px; text-align: right; border: 1px solid #ddd; font-size: 12px;">₹{ch_data.get('cogs', 0):,.2f}</td>
                        <td style="padding: 7px; text-align: right; border: 1px solid #ddd; font-size: 12px;">₹{ch_data.get('spend', 0):,.2f}</td>
                        <td style="padding: 7px; text-align: right; border: 1px solid #ddd; font-size: 12px; {profit_bg}">₹{profit:,.2f}</td>
                        <td style="padding: 7px; text-align: right; border: 1px solid #ddd; font-size: 12px; {roas_bg}">{net_roas:.2f}</td>
                        <td style="padding: 7px; text-align: right; border: 1px solid #ddd; font-size: 12px;">{ch_data.get('orders', 0):,}</td>
                        <td style="padding: 7px; text-align: right; border: 1px solid #ddd; font-size: 12px;">{ch_data.get('quantity', 0):,}</td>
                    </tr>
                """)
        
        # Add Amazon row if metrics are available for this timeframe
        amazon_metrics = amazon_wtd if timeframe_key == 'wtd' else amazon_mtd
        if amazon_metrics and amazon_metrics.get('available', False):
            amazon_revenue = amazon_metrics.get('revenue', 0)
            amazon_spend = amazon_metrics.get('spend', 0)
            amazon_orders = amazon_metrics.get('orders', 0)
            amazon_date_range = amazon_metrics.get('date_range', 'N/A')

            # P&L-derived fields (only populated when gold.fct_amazon_sp_order_pnl
            # has data for this range). Net Profit / Net ROAS use the same
            # formula as the other channels for an apples-to-apples comparison:
            #   net_profit = revenue - cogs - spend
            #   net_roas   = (revenue - cogs) / spend
            pnl_available = amazon_metrics.get('pnl_available', False)
            amazon_cogs = amazon_metrics.get('cogs', 0) if pnl_available else None
            amazon_net_profit = amazon_metrics.get('net_profit', 0) if pnl_available else None
            amazon_net_roas = amazon_metrics.get('net_roas', 0) if pnl_available else None
            amazon_units = amazon_metrics.get('units', 0) if pnl_available else None

            # Cell formatting: green/red tint on profit & ROAS, grey "N/A" when
            # the P&L view is not populated (preserves the previous look for
            # ranges with no settlement data).
            na_cell = (
                '<td style="padding: 7px; text-align: right; border: 1px solid #ddd; '
                'font-size: 12px; color: #999;">N/A</td>'
            )

            if amazon_cogs is not None:
                cogs_cell = (
                    f'<td style="padding: 7px; text-align: right; border: 1px solid #ddd; '
                    f'font-size: 12px;">₹{amazon_cogs:,.2f}</td>'
                )
            else:
                cogs_cell = na_cell

            if amazon_net_profit is not None:
                profit_bg = (
                    "background-color: #e8f5e9;" if amazon_net_profit > 0
                    else ("background-color: #ffebee;" if amazon_net_profit < 0 else "")
                )
                profit_cell = (
                    f'<td style="padding: 7px; text-align: right; border: 1px solid #ddd; '
                    f'font-size: 12px; {profit_bg}">₹{amazon_net_profit:,.2f}</td>'
                )
            else:
                profit_cell = na_cell

            if amazon_net_roas is not None and amazon_spend > 0:
                roas_bg = (
                    "background-color: #e8f5e9;" if amazon_net_roas >= 1
                    else "background-color: #ffebee;"
                )
                roas_cell = (
                    f'<td style="padding: 7px; text-align: right; border: 1px solid #ddd; '
                    f'font-size: 12px; {roas_bg}">{amazon_net_roas:.2f}</td>'
                )
            else:
                roas_cell = na_cell

            units_cell = (
                f'<td style="padding: 7px; text-align: right; border: 1px solid #ddd; '
                f'font-size: 12px;">{amazon_units:,}</td>'
                if amazon_units is not None else na_cell
            )

            html_parts.append(f"""
                <tr style="background-color: #FFF9E6;">
                    <td style="padding: 7px; border: 1px solid #ddd; font-size: 12px;">Amazon</td>
                    <td style="padding: 7px; text-align: right; border: 1px solid #ddd; font-size: 12px;">₹{amazon_revenue:,.2f}</td>
                    {cogs_cell}
                    <td style="padding: 7px; text-align: right; border: 1px solid #ddd; font-size: 12px;">₹{amazon_spend:,.2f}</td>
                    {profit_cell}
                    {roas_cell}
                    <td style="padding: 7px; text-align: right; border: 1px solid #ddd; font-size: 12px;">{amazon_orders:,}</td>
                    {units_cell}
                </tr>
            """)
        
        # Add total row
        html_parts.append(f"""
                    <tr style="border-top: 2px solid #333;">
                        <td style="padding: 8px; border: 1px solid #ddd; font-size: 12px; font-weight: 600;">TOTAL</td>
                        <td style="padding: 8px; text-align: right; border: 1px solid #ddd; font-size: 12px; font-weight: 600;">₹{total_revenue:,.2f}</td>
                        <td style="padding: 8px; text-align: right; border: 1px solid #ddd; font-size: 12px; font-weight: 600;">₹{total_cogs:,.2f}</td>
                        <td style="padding: 8px; text-align: right; border: 1px solid #ddd; font-size: 12px; font-weight: 600;">₹{total_spend:,.2f}</td>
                        <td style="padding: 8px; text-align: right; border: 1px solid #ddd; font-size: 12px; font-weight: 600;">₹{total_net_profit:,.2f}</td>
                        <td style="padding: 8px; text-align: right; border: 1px solid #ddd; font-size: 12px; font-weight: 600;">{total_net_roas:.2f}</td>
                        <td style="padding: 8px; text-align: right; border: 1px solid #ddd; font-size: 12px; font-weight: 600;">{total_orders:,}</td>
                        <td style="padding: 8px; text-align: right; border: 1px solid #ddd; font-size: 12px; font-weight: 600;">{total_quantity:,}</td>
                    </tr>
                </tbody>
            </table>
        """)
        
        # Add separator between WTD and MTD
        html_parts.append("""<hr style="border: none; border-top: 2px solid #ddd; margin: 25px 0;">""")
    
    return '\n'.join(html_parts)

def extract_daily_campaign_performers(daily_file_path: str) -> dict:
    """
    Extract top and bottom campaign performers from daily Excel report.
    Reads the meta_campaigns and google_campaigns sheets from the daily report.
    
    Ranking Logic:
    - Primary sort: Revenue (descending) - campaigns with higher revenue rank higher
    - Secondary sort: Net Profit (descending) - for campaigns with similar revenue
    - This ensures high-revenue campaigns are prioritized, with profitability as tiebreaker
    
    Args:
        daily_file_path: Path to daily Excel report file
    
    Returns:
        dict: Campaign performers data for Meta Ads and Google Ads
    """
    performers = {
        'meta_ads': {'top': [], 'bottom': []},
        'google_ads': {'top': [], 'bottom': []}
    }
    
    if not daily_file_path or not os.path.exists(daily_file_path):
        return performers
    
    try:
        # Read Meta campaigns sheet
        try:
            meta_df = pd.read_excel(daily_file_path, sheet_name='meta_campaigns')
            
            # Filter out Grand Total rows
            meta_df = meta_df[meta_df['campaign_name'] != 'Grand Total']
            
            # Map shopify_revenue to revenue if needed
            if 'shopify_revenue' in meta_df.columns and 'revenue' not in meta_df.columns:
                meta_df['revenue'] = meta_df['shopify_revenue']
            if 'shopify_cogs' in meta_df.columns and 'cogs' not in meta_df.columns:
                meta_df['cogs'] = meta_df['shopify_cogs']
            if 'shopify_orders' in meta_df.columns and 'orders' not in meta_df.columns:
                meta_df['orders'] = meta_df['shopify_orders']
            
            if not meta_df.empty and 'revenue' in meta_df.columns:
                # Calculate net profit and net ROAS first
                meta_df['net_profit'] = (
                    meta_df.get('revenue', 0) - 
                    meta_df.get('cogs', 0) - 
                    meta_df.get('spend', 0)
                )
                meta_df['net_roas'] = (
                    (meta_df.get('revenue', 0) - meta_df.get('cogs', 0)) / 
                    meta_df.get('spend', 1)
                ).replace([float('inf'), float('-inf')], 0).fillna(0)
                
                # Sort by revenue (primary) and net profit (secondary) - both descending
                meta_sorted = meta_df.sort_values(['revenue', 'net_profit'], ascending=[False, False])
                
                logger.info(f"Found {len(meta_sorted)} Meta campaigns in daily report (sorted by revenue, then net profit)")
                
                # Top 3
                top_3 = meta_sorted.head(3)
                for _, row in top_3.iterrows():
                    performers['meta_ads']['top'].append({
                        'name': row.get('campaign_name', 'Unknown'),
                        'revenue': float(row.get('revenue', 0)),
                        'spend': float(row.get('spend', 0)),
                        'net_profit': float(row.get('net_profit', 0)),
                        'net_roas': float(row.get('net_roas', 0)),
                        'orders': int(row.get('orders', 0))
                    })
                
                logger.info(f"Meta Ads - Top {len(performers['meta_ads']['top'])} campaigns extracted")
                
                # Bottom performers logic:
                # - If 1-2 campaigns: No bottom performers (all are top)
                # - If 3 campaigns: Show bottom 1
                # - If 4+ campaigns: Show bottom 3 (avoid overlap with top 3)
                num_campaigns = len(meta_sorted)
                if num_campaigns >= 3:
                    # Determine how many bottom performers to show
                    if num_campaigns == 3:
                        bottom_count = 1  # Show only the bottom 1 to avoid overlap
                    else:
                        bottom_count = min(3, num_campaigns - 3)  # Show up to 3, but avoid overlap with top 3
                    
                    if bottom_count > 0:
                        bottom_campaigns = meta_sorted.tail(bottom_count)
                        for _, row in bottom_campaigns.iterrows():
                            performers['meta_ads']['bottom'].append({
                                'name': row.get('campaign_name', 'Unknown'),
                                'revenue': float(row.get('revenue', 0)),
                                'spend': float(row.get('spend', 0)),
                                'net_profit': float(row.get('net_profit', 0)),
                                'net_roas': float(row.get('net_roas', 0)),
                                'orders': int(row.get('orders', 0))
                            })
                
                logger.info(f"Meta Ads - Bottom {len(performers['meta_ads']['bottom'])} campaigns extracted")
        except Exception as e:
            logger.warning(f"Could not read Meta campaigns from daily report: {e}")
        
        # Read Google campaigns sheet
        try:
            google_df = pd.read_excel(daily_file_path, sheet_name='google_campaigns')
            
            # Filter out Grand Total rows
            google_df = google_df[google_df['campaign_name'] != 'Grand Total']
            
            # Map shopify_revenue to revenue if needed
            if 'shopify_revenue' in google_df.columns and 'revenue' not in google_df.columns:
                google_df['revenue'] = google_df['shopify_revenue']
            if 'shopify_cogs' in google_df.columns and 'cogs' not in google_df.columns:
                google_df['cogs'] = google_df['shopify_cogs']
            if 'shopify_orders' in google_df.columns and 'orders' not in google_df.columns:
                google_df['orders'] = google_df['shopify_orders']
            
            if not google_df.empty and 'revenue' in google_df.columns:
                # Deduplicate by campaign_name (aggregate SKU rows)
                google_agg = google_df.groupby('campaign_name').agg({
                    'revenue': 'first',
                    'spend': 'first',
                    'cogs': 'first',
                    'orders': 'first'
                }).reset_index()
                
                # Calculate net profit and net ROAS
                google_agg['net_profit'] = (
                    google_agg.get('revenue', 0) - 
                    google_agg.get('cogs', 0) - 
                    google_agg.get('spend', 0)
                )
                google_agg['net_roas'] = (
                    (google_agg.get('revenue', 0) - google_agg.get('cogs', 0)) / 
                    google_agg.get('spend', 1)
                ).replace([float('inf'), float('-inf')], 0).fillna(0)
                
                # Sort by revenue (primary) and net profit (secondary) - both descending
                google_sorted = google_agg.sort_values(['revenue', 'net_profit'], ascending=[False, False])
                
                logger.info(f"Found {len(google_sorted)} Google campaigns in daily report (sorted by revenue, then net profit)")
                
                # Top 3
                top_3 = google_sorted.head(3)
                for _, row in top_3.iterrows():
                    performers['google_ads']['top'].append({
                        'name': row.get('campaign_name', 'Unknown'),
                        'revenue': float(row.get('revenue', 0)),
                        'spend': float(row.get('spend', 0)),
                        'net_profit': float(row.get('net_profit', 0)),
                        'net_roas': float(row.get('net_roas', 0)),
                        'orders': int(row.get('orders', 0))
                    })
                
                logger.info(f"Google Ads - Top {len(performers['google_ads']['top'])} campaigns extracted")
                
                # Bottom performers logic:
                # - If 1-2 campaigns: No bottom performers (all are top)
                # - If 3 campaigns: Show bottom 1
                # - If 4+ campaigns: Show bottom 3 (avoid overlap with top 3)
                num_campaigns = len(google_sorted)
                if num_campaigns >= 3:
                    # Determine how many bottom performers to show
                    if num_campaigns == 3:
                        bottom_count = 1  # Show only the bottom 1 to avoid overlap
                    else:
                        bottom_count = min(3, num_campaigns - 3)  # Show up to 3, but avoid overlap with top 3
                    
                    if bottom_count > 0:
                        bottom_campaigns = google_sorted.tail(bottom_count)
                        for _, row in bottom_campaigns.iterrows():
                            performers['google_ads']['bottom'].append({
                                'name': row.get('campaign_name', 'Unknown'),
                                'revenue': float(row.get('revenue', 0)),
                                'spend': float(row.get('spend', 0)),
                                'net_profit': float(row.get('net_profit', 0)),
                                'net_roas': float(row.get('net_roas', 0)),
                                'orders': int(row.get('orders', 0))
                            })
                
                logger.info(f"Google Ads - Bottom {len(performers['google_ads']['bottom'])} campaigns extracted")
        except Exception as e:
            logger.warning(f"Could not read Google campaigns from daily report: {e}")
        
    except Exception as e:
        logger.error(f"Error extracting daily campaign performers: {e}")
    
    # Log final results
    logger.info(f"Daily performers extraction complete:")
    logger.info(f"  Meta Ads - Top: {len(performers['meta_ads']['top'])}, Bottom: {len(performers['meta_ads']['bottom'])}")
    logger.info(f"  Google Ads - Top: {len(performers['google_ads']['top'])}, Bottom: {len(performers['google_ads']['bottom'])}")
    
    return performers

def format_daily_performers_html(performers: dict, yesterday_str: str) -> str:
    """
    Format daily campaign performers as HTML for email.
    
    Args:
        performers: Dictionary containing top/bottom performers for Meta and Google
        yesterday_str: Date string for yesterday (e.g., '2025-10-09')
    
    Returns:
        str: HTML formatted campaign performers section
    """
    html_parts = []
    
    channel_names = {
        'meta_ads': 'Meta Ads',
        'google_ads': 'Google Ads'
    }
    
    # Add title with ranking logic explanation
    html_parts.append(f"""
        <h3 style="color: #333; border-bottom: 1px solid #ddd; padding-bottom: 8px; margin-top: 30px; font-weight: 600;">Daily Campaign Performers ({yesterday_str})</h3>
        <div style="background-color: #f9f9f9; border-left: 3px solid #4472C4; padding: 10px; margin-bottom: 15px;">
            <p style="color: #666; font-size: 12px; margin: 0;">
                <strong>Ranking Logic:</strong> Campaigns are ranked by <strong>Revenue</strong> (primary) and <strong>Net Profit</strong> (secondary).<br/>
                <em style="font-size: 11px;">High-revenue campaigns are prioritized, with profitability used as a tiebreaker for campaigns with similar revenue.</em>
            </p>
        </div>
    """)
    
    # Process each channel
    for channel_key in ['meta_ads', 'google_ads']:
        if channel_key not in performers:
            continue
        
        channel_data = performers[channel_key]
        top_campaigns = channel_data.get('top', [])
        bottom_campaigns = channel_data.get('bottom', [])
        channel_display = channel_names.get(channel_key, channel_key)
        
        logger.info(f"Formatting {channel_key} performers: {len(top_campaigns)} top, {len(bottom_campaigns)} bottom")
        
        if not top_campaigns and not bottom_campaigns:
            logger.info(f"No performers found for {channel_key}, skipping")
            continue
        
        html_parts.append(f"""
            <h4 style="color: #555; margin-top: 18px; margin-bottom: 8px; font-weight: 600; font-size: 14px;">{channel_display}</h4>
        """)
        
        # Top performers
        if top_campaigns:
            logger.info(f"Rendering {len(top_campaigns)} top performers for {channel_key}")
            html_parts.append("""
            <p style="margin-bottom: 8px; font-size: 13px; font-weight: 600;">Top Performers</p>
            <table style="border-collapse: collapse; width: 100%; margin-bottom: 15px; border: 1px solid #ddd;">
                <thead>
                    <tr style="background-color: #f5f5f5;">
                        <th style="padding: 7px; text-align: left; border: 1px solid #ddd; font-size: 12px; font-weight: 600;">Campaign</th>
                        <th style="padding: 7px; text-align: right; border: 1px solid #ddd; font-size: 12px; font-weight: 600;">Revenue ↓</th>
                        <th style="padding: 7px; text-align: right; border: 1px solid #ddd; font-size: 12px; font-weight: 600;">Net Profit ↓</th>
                        <th style="padding: 7px; text-align: right; border: 1px solid #ddd; font-size: 12px; font-weight: 600;">Spend</th>
                        <th style="padding: 7px; text-align: right; border: 1px solid #ddd; font-size: 12px; font-weight: 600;">Net ROAS</th>
                    </tr>
                </thead>
                <tbody>
            """)
            
            for campaign in top_campaigns:
                profit = campaign['net_profit']
                profit_bg = "background-color: #e8f5e9;" if profit > 0 else ("background-color: #ffebee;" if profit < 0 else "")
                
                roas = campaign['net_roas']
                roas_bg = "background-color: #e8f5e9;" if roas >= 1 else "background-color: #ffebee;"
                
                html_parts.append(f"""
                    <tr>
                        <td style="padding: 6px; border: 1px solid #ddd; font-size: 12px;">{campaign['name']}</td>
                        <td style="padding: 6px; text-align: right; border: 1px solid #ddd; font-size: 12px;">₹{campaign['revenue']:,.2f}</td>
                        <td style="padding: 6px; text-align: right; border: 1px solid #ddd; font-size: 12px; {profit_bg}">₹{profit:,.2f}</td>
                        <td style="padding: 6px; text-align: right; border: 1px solid #ddd; font-size: 12px;">₹{campaign['spend']:,.2f}</td>
                        <td style="padding: 6px; text-align: right; border: 1px solid #ddd; font-size: 12px; {roas_bg}">{roas:.2f}</td>
                    </tr>
                """)
            
            html_parts.append("""
                </tbody>
            </table>
            """)
        
        # Bottom performers
        if bottom_campaigns:
            logger.info(f"Rendering {len(bottom_campaigns)} bottom performers for {channel_key}")
            html_parts.append("""
            <p style="margin-bottom: 8px; font-size: 13px; font-weight: 600;">Bottom Performers</p>
            <table style="border-collapse: collapse; width: 100%; margin-bottom: 15px; border: 1px solid #ddd;">
                <thead>
                    <tr style="background-color: #f5f5f5;">
                        <th style="padding: 7px; text-align: left; border: 1px solid #ddd; font-size: 12px; font-weight: 600;">Campaign</th>
                        <th style="padding: 7px; text-align: right; border: 1px solid #ddd; font-size: 12px; font-weight: 600;">Revenue ↓</th>
                        <th style="padding: 7px; text-align: right; border: 1px solid #ddd; font-size: 12px; font-weight: 600;">Net Profit ↓</th>
                        <th style="padding: 7px; text-align: right; border: 1px solid #ddd; font-size: 12px; font-weight: 600;">Spend</th>
                        <th style="padding: 7px; text-align: right; border: 1px solid #ddd; font-size: 12px; font-weight: 600;">Net ROAS</th>
                    </tr>
                </thead>
                <tbody>
            """)
            
            for campaign in bottom_campaigns:
                profit = campaign['net_profit']
                profit_bg = "background-color: #e8f5e9;" if profit > 0 else ("background-color: #ffebee;" if profit < 0 else "")
                
                roas = campaign['net_roas']
                roas_bg = "background-color: #e8f5e9;" if roas >= 1 else "background-color: #ffebee;"
                
                html_parts.append(f"""
                    <tr>
                        <td style="padding: 6px; border: 1px solid #ddd; font-size: 12px;">{campaign['name']}</td>
                        <td style="padding: 6px; text-align: right; border: 1px solid #ddd; font-size: 12px;">₹{campaign['revenue']:,.2f}</td>
                        <td style="padding: 6px; text-align: right; border: 1px solid #ddd; font-size: 12px; {profit_bg}">₹{profit:,.2f}</td>
                        <td style="padding: 6px; text-align: right; border: 1px solid #ddd; font-size: 12px;">₹{campaign['spend']:,.2f}</td>
                        <td style="padding: 6px; text-align: right; border: 1px solid #ddd; font-size: 12px; {roas_bg}">{roas:.2f}</td>
                    </tr>
                """)
            
            html_parts.append("""
                </tbody>
            </table>
            """)
        else:
            logger.info(f"No bottom performers to render for {channel_key}")
    
    if not html_parts or len(html_parts) == 2:  # Only title and description
        logger.warning("No campaign performer data available to display")
        return ""
    
    return '\n'.join(html_parts)

def send_wtd_mtd_email(wtd_mtd_file_path: str, daily_file_path: str, amazon_file_path: str, summary_data: dict, pdf_paths: dict = None, product_profitability_file_path: str = None):
    """
    Send WTD/MTD report, daily report, Amazon report, and optional Product Profitability report via email using Microsoft Graph API.
    Similar to send_email() in excel_generation.py but simplified for WTD/MTD reports.
    
    Args:
        wtd_mtd_file_path: Path to the generated WTD/MTD Excel report
        daily_file_path: Path to the generated daily Excel report for previous day
        amazon_file_path: Path to the generated Amazon Excel report for 2 days earlier
        summary_data: Dictionary containing summary metrics for WTD and MTD
        pdf_paths: Dictionary with 'wtd' and 'mtd' keys containing paths to PDF reports
        product_profitability_file_path: Optional path to Product Profitability Excel (previous day)
    """
    if not EMAIL_ENABLED:
        logger.warning("Email sending is disabled due to incomplete configuration")
        return
    
    # Validate WTD/MTD file
    if not wtd_mtd_file_path or not os.path.exists(wtd_mtd_file_path):
        logger.error(f"WTD/MTD Excel file not found: {wtd_mtd_file_path}")
        return
    
    # Validate daily file (optional - continue without it if not available)
    if not daily_file_path or not os.path.exists(daily_file_path):
        logger.warning(f"Daily Excel file not found: {daily_file_path}, continuing without it")
        daily_file_path = None
    
    # Validate Amazon file (optional - continue without it if not available)
    if not amazon_file_path or not os.path.exists(amazon_file_path):
        logger.warning(f"Amazon Excel file not found: {amazon_file_path}, continuing without it")
        amazon_file_path = None
    
    try:
        logger.info("Preparing to send WTD/MTD report via Microsoft Graph API...")
        
        # Acquire access token
        logger.info(f"Using Tenant ID: {TENANT_ID}, Client ID: {CLIENT_ID[:10]}...")
        authority = f'https://login.microsoftonline.com/{TENANT_ID}'
        scope = ["https://graph.microsoft.com/.default"]
        app = msal.ConfidentialClientApplication(CLIENT_ID, authority=authority, client_credential=CLIENT_SECRET)
        result = app.acquire_token_for_client(scopes=scope)
        print(result)
        
        if "access_token" not in result:
            logger.error(f"Failed to acquire access token. Full result: {result}")
            raise Exception("Failed to acquire access token for Microsoft Graph API")
        
        access_token = result["access_token"]
        
        # Prepare email subject and body
        now = datetime.now(IST)
        today_str = now.strftime('%Y-%m-%d')
        yesterday = (now - timedelta(days=1))
        yesterday_str = yesterday.strftime('%Y-%m-%d')
        two_days_ago = (now - timedelta(days=2))
        two_days_ago_str = two_days_ago.strftime('%Y-%m-%d')
        weekday = now.strftime('%A')
        
        subject = f"WTD/MTD, Daily & Amazon Performance Report - {today_str}"
        
        # Extract daily efficiency metrics for comparison table
        daily_metrics = None
        if daily_file_path:
            try:
                daily_metrics = extract_daily_efficiency_metrics(daily_file_path)
                logger.info(f"Extracted daily metrics: CPO=₹{daily_metrics['cost_per_order']:.2f}, CPU=₹{daily_metrics['cost_per_unit']:.2f}, AOV=₹{daily_metrics['avg_order_value']:.2f}")
            except Exception as e:
                logger.warning(f"Could not extract daily efficiency metrics: {e}")
        
        # Extract Amazon metrics for WTD and MTD date ranges.
        # Amazon uses CALENDAR-based windows (Mon-to-today / 1st-to-yesterday),
        # then WTD end is shifted back 1 day to account for gold-table lag.
        amazon_wtd = None
        amazon_mtd = None
        try:
            wtd_timeframe = summary_data.get('wtd', {})
            mtd_timeframe = summary_data.get('mtd', {})

            amazon_timeframes = get_amazon_calendar_timeframes()

            # Fetch Amazon data for WTD (calendar week start -> yesterday)
            wtd_start = amazon_timeframes['wtd']['start_date']
            wtd_end = amazon_timeframes['wtd']['end_date'] - timedelta(days=1)
            amazon_wtd = get_amazon_summary_metrics(wtd_start, wtd_end)
            if amazon_wtd.get('available', False):
                logger.info(f"Extracted Amazon WTD metrics: Revenue=₹{amazon_wtd['revenue']:.2f}, Spend=₹{amazon_wtd['spend']:.2f}, Orders={amazon_wtd['orders']}, Range={amazon_wtd['date_range']}")
            else:
                logger.warning("No Amazon data available for WTD")

            # Fetch Amazon data for MTD (1st of month -> yesterday)
            mtd_start = amazon_timeframes['mtd']['start_date']
            mtd_end = amazon_timeframes['mtd']['end_date']
            amazon_mtd = get_amazon_summary_metrics(mtd_start, mtd_end)
            if amazon_mtd.get('available', False):
                logger.info(f"Extracted Amazon MTD metrics: Revenue=₹{amazon_mtd['revenue']:.2f}, Spend=₹{amazon_mtd['spend']:.2f}, Orders={amazon_mtd['orders']}, Range={amazon_mtd['date_range']}")
            else:
                logger.warning("No Amazon data available for MTD")
        except Exception as e:
            logger.warning(f"Could not extract Amazon metrics: {e}")
        
        # Format summary data for email (includes efficiency comparison table and Amazon data)
        summary_html = format_summary_for_email(summary_data, amazon_wtd, amazon_mtd, daily_metrics)
        
        # Extract and format daily campaign performers
        daily_performers_html = ""
        if daily_file_path:
            try:
                daily_performers = extract_daily_campaign_performers(daily_file_path)
                daily_performers_html = format_daily_performers_html(daily_performers, yesterday_str)
            except Exception as e:
                logger.warning(f"Could not extract daily performers: {e}")
        
        email_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333; max-width: 1200px; margin: 0 auto;">
            <p style="font-size: 14px;">Dear Team,</p>
            <p style="font-size: 14px;">Please find attached the marketing performance reports for {today_str} ({weekday}):</p>
            <ul style="font-size: 14px;">
                <li><strong>WTD/MTD Report:</strong> Week-to-Date, Month-to-Date, and Daily entity summary</li>
                <li><strong>Daily Report:</strong> Detailed report for {yesterday_str}</li>
                <li><strong>Amazon Report:</strong> Campaign performance for {two_days_ago_str} (2 days earlier)</li>
                <li><strong>Product Profitability:</strong> Product profitability for {yesterday_str}</li>
            </ul>
            
            {summary_html}
            
            {daily_performers_html}
            
            <p style="font-size: 12px; color: #666; margin-top: 30px; border-top: 1px solid #ddd; padding-top: 15px;">
                This is an automated report. For questions, please contact the marketing team.
            </p>
        </body>
        </html>
        """
        
        # Prepare attachments list
        attachments = []
        
        # Add WTD/MTD report attachment
        with open(wtd_mtd_file_path, "rb") as f:
            wtd_mtd_content = base64.b64encode(f.read()).decode('utf-8')
        
        wtd_mtd_attachment = {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": f"WTD_MTD_Entity_Report_{today_str}.xlsx",
            "contentBytes": wtd_mtd_content
        }
        attachments.append(wtd_mtd_attachment)
        
        # Add daily report attachment if available
        if daily_file_path:
            with open(daily_file_path, "rb") as f:
                daily_content = base64.b64encode(f.read()).decode('utf-8')
            
            daily_attachment = {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": f"Daily_Entity_Report_{yesterday_str}.xlsx",
                "contentBytes": daily_content
            }
            attachments.append(daily_attachment)
            logger.info(f"Added daily report to email: {daily_file_path}")
        
        # Add Amazon report attachment if available
        if amazon_file_path:
            with open(amazon_file_path, "rb") as f:
                amazon_content = base64.b64encode(f.read()).decode('utf-8')
            
            amazon_attachment = {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": f"Amazon_Campaign_Report_{two_days_ago_str}.xlsx",
                "contentBytes": amazon_content
            }
            attachments.append(amazon_attachment)
            logger.info(f"Added Amazon report to email: {amazon_file_path}")
        
        # Add Product Profitability report attachment if available (previous day)
        if product_profitability_file_path and os.path.exists(product_profitability_file_path):
            try:
                with open(product_profitability_file_path, "rb") as f:
                    pp_content = base64.b64encode(f.read()).decode('utf-8')
                pp_attachment = {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": f"Product_Profitability_{yesterday_str}.xlsx",
                    "contentBytes": pp_content
                }
                attachments.append(pp_attachment)
                logger.info(f"Added Product Profitability report to email: {product_profitability_file_path}")
            except Exception as e:
                logger.warning(f"Failed to add Product Profitability to email: {e}")
        
        # Add WTD PDF attachment if available
        if pdf_paths and pdf_paths.get('wtd') and os.path.exists(pdf_paths['wtd']):
            try:
                with open(pdf_paths['wtd'], "rb") as f:
                    wtd_pdf_content = base64.b64encode(f.read()).decode('utf-8')
                
                wtd_pdf_attachment = {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": f"WTD_Meta_Campaigns_Report_{today_str}.pdf",
                    "contentBytes": wtd_pdf_content
                }
                attachments.append(wtd_pdf_attachment)
                logger.info(f"Added WTD PDF report to email: {pdf_paths['wtd']}")
            except Exception as e:
                logger.warning(f"Failed to add WTD PDF to email: {e}")
        
        # Add MTD PDF attachment if available
        if pdf_paths and pdf_paths.get('mtd') and os.path.exists(pdf_paths['mtd']):
            try:
                with open(pdf_paths['mtd'], "rb") as f:
                    mtd_pdf_content = base64.b64encode(f.read()).decode('utf-8')
                
                mtd_pdf_attachment = {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": f"MTD_Meta_Campaigns_Report_{today_str}.pdf",
                    "contentBytes": mtd_pdf_content
                }
                attachments.append(mtd_pdf_attachment)
                logger.info(f"Added MTD PDF report to email: {pdf_paths['mtd']}")
            except Exception as e:
                logger.warning(f"Failed to add MTD PDF to email: {e}")
        
        # Prepare email message
        message = {
            "message": {
                "subject": subject,
                "body": {
                    "contentType": "HTML",
                    "content": email_body
                },
                "toRecipients": [{"emailAddress": {"address": addr.strip()}} for addr in EMAIL_RECIPIENTS if addr.strip()],
                "attachments": attachments,
            },
            "saveToSentItems": "true"
        }
        
        # Send email via Microsoft Graph API
        logger.info("Sending email via Microsoft Graph API...")
        graph_url = f"https://graph.microsoft.com/v1.0/users/{EMAIL_SENDER}/sendMail"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }
        
        response = requests.post(graph_url, headers=headers, json=message)
        
        if response.status_code == 202:
            logger.info(f"Email sent successfully to {len(EMAIL_RECIPIENTS)} recipients")
            
            # Delete the Excel files after successful email send
            try:
                os.remove(wtd_mtd_file_path)
                logger.info(f"Deleted WTD/MTD Excel file after sending email: {wtd_mtd_file_path}")
            except Exception as e:
                logger.warning(f"Failed to delete WTD/MTD Excel file {wtd_mtd_file_path}: {e}")
            
            if daily_file_path:
                try:
                    os.remove(daily_file_path)
                    logger.info(f"Deleted daily Excel file after sending email: {daily_file_path}")
                except Exception as e:
                    logger.warning(f"Failed to delete daily Excel file {daily_file_path}: {e}")
            
            if amazon_file_path:
                try:
                    os.remove(amazon_file_path)
                    logger.info(f"Deleted Amazon Excel file after sending email: {amazon_file_path}")
                except Exception as e:
                    logger.warning(f"Failed to delete Amazon Excel file {amazon_file_path}: {e}")
            
            # Delete PDF files after successful email send
            if pdf_paths:
                if pdf_paths.get('wtd') and os.path.exists(pdf_paths['wtd']):
                    try:
                        os.remove(pdf_paths['wtd'])
                        logger.info(f"Deleted WTD PDF file after sending email: {pdf_paths['wtd']}")
                    except Exception as e:
                        logger.warning(f"Failed to delete WTD PDF file {pdf_paths['wtd']}: {e}")
                
                if pdf_paths.get('mtd') and os.path.exists(pdf_paths['mtd']):
                    try:
                        os.remove(pdf_paths['mtd'])
                        logger.info(f"Deleted MTD PDF file after sending email: {pdf_paths['mtd']}")
                    except Exception as e:
                        logger.warning(f"Failed to delete MTD PDF file {pdf_paths['mtd']}: {e}")
        else:
            logger.error(f"Failed to send email: {response.status_code} {response.text}")
            raise Exception(f"Failed to send email: {response.status_code} {response.text}")
            
    except Exception as e:
        logger.error(f"Error sending WTD/MTD email: {str(e)}")
        raise


def generate_wtd_mtd_pdf_reports(out_dir: str = None) -> dict:
    """
    Generate PDF reports for WTD and MTD timeframes for Meta campaigns.
    
    Args:
        out_dir: Output directory for PDF files
        
    Returns:
        dict: Dictionary with 'wtd' and 'mtd' keys, each containing the path to the generated PDF
    """
    if out_dir is None:
        out_dir = get_report_dir()
    os.makedirs(out_dir, exist_ok=True)
    
    # Get WTD and MTD timeframes
    timeframes = get_wtd_mtd_timeframes()
    
    pdf_paths = {}
    
    for timeframe_key, timeframe_config in timeframes.items():
        start_date = timeframe_config['start_date']
        end_date = timeframe_config['end_date']
        label = timeframe_config['label']
        
        logger.info(f"Generating {label} PDF report for {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
        
        try:
            # Get campaign data for this timeframe
            start_date_str = start_date.strftime('%Y-%m-%d')
            end_date_str = end_date.strftime('%Y-%m-%d')
            
            campaign_df = get_campaign_data(start_date=start_date_str, end_date=end_date_str)
            
            logger.info(f"[{label}] Campaign data shape: {campaign_df.shape}")
            
            # Prepare metrics dict similar to what process_data returns
            if not campaign_df.empty:
                # Ensure required columns exist
                if 'sales' not in campaign_df.columns:
                    if 'shopify_revenue' in campaign_df.columns:
                        campaign_df['sales'] = campaign_df['shopify_revenue']
                    elif 'revenue' in campaign_df.columns:
                        campaign_df['sales'] = campaign_df['revenue']
                    else:
                        campaign_df['sales'] = 0
                
                if 'shopify_revenue' not in campaign_df.columns:
                    if 'sales' in campaign_df.columns:
                        campaign_df['shopify_revenue'] = campaign_df['sales']
                    elif 'revenue' in campaign_df.columns:
                        campaign_df['shopify_revenue'] = campaign_df['revenue']
                    else:
                        campaign_df['shopify_revenue'] = 0
                
                # Ensure other required columns exist
                required_cols = ['spend', 'cogs', 'net_profit', 'purchases', 'clicks', 'impressions', 
                               'ctr', 'bounce_rate', 'conversion_rate', 'gross_roas', 'net_roas', 
                               'be_roas', 'breakeven_roas']
                for col in required_cols:
                    if col not in campaign_df.columns:
                        campaign_df[col] = 0
                
                # Segregate PMF campaigns
                pmf_campaigns = campaign_df[campaign_df['campaign_name'].str.contains('PMF', case=False, na=False)].copy()
                non_pmf_campaigns = campaign_df[~campaign_df['campaign_name'].str.contains('PMF', case=False, na=False)].copy()
                
                # Sort campaigns
                if not pmf_campaigns.empty:
                    pmf_campaigns = pmf_campaigns.sort_values('net_roas', ascending=False)
                
                sorted_campaigns = campaign_df.sort_values('net_roas', ascending=False)
                high_roas_campaigns = sorted_campaigns[sorted_campaigns['net_roas'] > 1]
                
                # Calculate overall metrics
                total_sales = campaign_df['sales'].sum() if 'sales' in campaign_df.columns else campaign_df['shopify_revenue'].sum() if 'shopify_revenue' in campaign_df.columns else 0
                total_ad_spend = campaign_df['spend'].sum()
                total_cogs = campaign_df['cogs'].sum() if 'cogs' in campaign_df.columns else 0
                total_net_profit = campaign_df['net_profit'].sum() if 'net_profit' in campaign_df.columns else (total_sales - total_cogs - total_ad_spend)
                total_clicks = campaign_df['clicks'].sum() if 'clicks' in campaign_df.columns else 0
                total_purchases = campaign_df['purchases'].sum() if 'purchases' in campaign_df.columns else 0
                total_impressions = campaign_df['impressions'].sum() if 'impressions' in campaign_df.columns else 0
                
                # Calculate overall metrics
                overall_roas = 0 if total_ad_spend == 0 else np.nan_to_num(total_sales / total_ad_spend, nan=0, posinf=0, neginf=0).round(2)
                overall_net_roas = 0 if total_ad_spend == 0 else np.nan_to_num((total_sales - total_cogs) / total_ad_spend, nan=0, posinf=0, neginf=0).round(2)
                overall_cpp = 0 if total_purchases == 0 else np.nan_to_num(total_ad_spend / total_purchases, nan=0, posinf=0, neginf=0).round(2)
                overall_ctr = 0 if total_impressions == 0 else np.nan_to_num((total_clicks / total_impressions) * 100, nan=0, posinf=0, neginf=0).round(2)
                overall_conversion_rate = 0 if total_clicks == 0 else np.nan_to_num((total_purchases / total_clicks) * 100, nan=0, posinf=0, neginf=0).round(2)
                overall_breakeven_roas = campaign_df['breakeven_roas'].mean() if 'breakeven_roas' in campaign_df.columns else 0
                
                metrics = {
                    'total_sales': total_sales,
                    'total_ad_spend': total_ad_spend,
                    'google_ad_spend': 0,  # WTD/MTD PDFs focus on Meta only
                    'total_cogs': total_cogs,
                    'overall_roas': overall_roas,
                    'overall_net_roas': overall_net_roas,
                    'overall_cpp': overall_cpp,
                    'overall_ctr': overall_ctr,
                    'overall_conversion_rate': overall_conversion_rate,
                    'total_impressions': total_impressions,
                    'total_clicks': total_clicks,
                    'total_conversions': total_purchases,
                    'campaign_summary': campaign_df,
                    'high_roas_campaigns': high_roas_campaigns,
                    'active_campaigns': sorted_campaigns,
                    'pmf_campaigns': pmf_campaigns,
                    'non_pmf_campaigns': non_pmf_campaigns,
                    'ad_level_report': None,
                    'total_net_profit': total_net_profit,
                    'total_margin': total_net_profit,
                    'overall_breakeven_roas': overall_breakeven_roas
                }
            else:
                # Empty campaign data - create empty metrics
                metrics = {
                    'total_sales': 0.0,
                    'total_ad_spend': 0.0,
                    'google_ad_spend': 0.0,
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
                    'ad_level_report': None,
                    'total_net_profit': 0.0,
                    'total_margin': 0.0,
                    'overall_breakeven_roas': 0.0
                }
            
            # Generate timestamp for filename
            timestamp_str = datetime.now().strftime('%Y%m%d_%H%M%S')
            
            # Use end_date as the "today" parameter for the PDF (represents the report date)
            pdf_file = generate_pdf_report(
                metrics,
                end_date.date(),  # Use end_date as the report date
                timestamp_str,
                timeframe_start=start_date,
                timeframe_end=end_date,
                report_type=timeframe_key  # Pass 'wtd' or 'mtd' as report type
            )
            
            pdf_paths[timeframe_key] = pdf_file
            logger.info(f"[{label}] PDF report generated: {pdf_file}")
            
        except Exception as e:
            logger.error(f"Error generating {label} PDF report: {str(e)}", exc_info=True)
            pdf_paths[timeframe_key] = None
    
    return pdf_paths


def main():
    """
    Main entry point for WTD/MTD report generation.
    This function is called by the Azure Function.
    Generates the WTD/MTD report, daily report for previous day, and Amazon report for 2 days earlier,
    then sends all three via email.
    
    Returns:
        str: Paths to the generated report files (or success message if emailed)
    """
    try:
        print("Generating WTD/MTD Entity Report...")
        logger.info("Starting WTD/MTD report generation...")
        
        # Generate the WTD/MTD report
        wtd_mtd_path, summary_data = run_wtd_mtd_report()
        print(f"WTD/MTD Report written to: {wtd_mtd_path}")
        logger.info(f"WTD/MTD Report generated successfully: {wtd_mtd_path}")
        
        # Generate WTD/MTD PDF reports for Meta campaigns
        pdf_paths = None
        try:
            print("Generating WTD/MTD PDF Reports for Meta campaigns...")
            logger.info("Starting WTD/MTD PDF report generation...")
            pdf_paths = generate_wtd_mtd_pdf_reports()
            if pdf_paths:
                if pdf_paths.get('wtd'):
                    print(f"WTD PDF Report written to: {pdf_paths['wtd']}")
                    logger.info(f"WTD PDF Report generated successfully: {pdf_paths['wtd']}")
                if pdf_paths.get('mtd'):
                    print(f"MTD PDF Report written to: {pdf_paths['mtd']}")
                    logger.info(f"MTD PDF Report generated successfully: {pdf_paths['mtd']}")
        except Exception as e:
            logger.error(f"Failed to generate WTD/MTD PDF reports: {e}")
            print(f"Warning: Could not generate WTD/MTD PDF reports, continuing without them")
            pdf_paths = None
        
        # Generate daily report for previous day
        daily_path = None
        try:
            print("Generating Daily Entity Report for previous day...")
            logger.info("Starting daily report generation for previous day...")
            
            # Calculate yesterday's date
            now = datetime.now(IST)
            yesterday = (now - timedelta(days=1)).strftime('%Y-%m-%d')
            
            # Generate daily report for yesterday using dailyrollup.py
            daily_path = run_daily_rollup(
                start_date=yesterday,
                end_date=yesterday,
                out_dir=None
            )
            print(f"Daily Report written to: {daily_path}")
            logger.info(f"Daily Report generated successfully: {daily_path}")
        except Exception as e:
            logger.error(f"Failed to generate daily report: {e}")
            print(f"Warning: Could not generate daily report, continuing without it")
            daily_path = None
        
        # Generate Amazon report for 2 days earlier
        amazon_path = None
        try:
            print("Generating Amazon Campaign Report for 2 days earlier...")
            logger.info("Starting Amazon report generation for 2 days earlier...")
            
            # Generate Amazon report
            amazon_path = run_amazon_report()
            
            if amazon_path:
                print(f"Amazon Report written to: {amazon_path}")
                logger.info(f"Amazon Report generated successfully: {amazon_path}")
            else:
                print("Warning: No Amazon data available for 2 days earlier")
                logger.warning("No Amazon data available for 2 days earlier")
        except Exception as e:
            logger.error(f"Failed to generate Amazon report: {e}")
            print(f"Warning: Could not generate Amazon report, continuing without it")
            amazon_path = None
        
        # Generate Product Profitability report for previous day (separate Excel)
        pp_path = None
        try:
            print("Generating Product Profitability report for previous day...")
            logger.info("Starting Product Profitability report for previous day...")
            pp_path = run_product_profitability_report(for_date=None, out_dir=None)
            print(f"Product Profitability Report written to: {pp_path}")
            logger.info(f"Product Profitability report generated successfully: {pp_path}")
        except Exception as e:
            logger.error(f"Failed to generate Product Profitability report: {e}")
            print(f"Warning: Could not generate Product Profitability report, continuing without it")
            pp_path = None
        
        # Send via email if enabled
        if EMAIL_ENABLED:
            try:
                logger.info("Sending WTD/MTD, daily, Amazon, and Product Profitability reports via email...")
                send_wtd_mtd_email(wtd_mtd_path, daily_path, amazon_path, summary_data, pdf_paths, product_profitability_file_path=pp_path)
                logger.info("Reports sent via email successfully")
                return "WTD/MTD, daily, Amazon, and Product Profitability reports generated and emailed successfully"
            except Exception as e:
                logger.error(f"Failed to send email, but reports were generated: {e}")
                return f"WTD/MTD: {wtd_mtd_path}, Daily: {daily_path if daily_path else 'Not generated'}, Amazon: {amazon_path if amazon_path else 'Not generated'}, PP: {pp_path if pp_path else 'Not generated'}"
        else:
            logger.warning("Email sending is disabled - reports saved locally only")
            return f"WTD/MTD: {wtd_mtd_path}, Daily: {daily_path if daily_path else 'Not generated'}, Amazon: {amazon_path if amazon_path else 'Not generated'}, PP: {pp_path if pp_path else 'Not generated'}"
            
    except Exception as e:
        logger.error(f"Error in WTD/MTD report generation: {e}")
        raise


if __name__ == '__main__':
    # Generate WTD/MTD report
    main()

