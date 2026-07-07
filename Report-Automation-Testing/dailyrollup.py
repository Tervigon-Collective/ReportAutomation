import json
import os
from datetime import datetime
import pandas as pd
import xlsxwriter
import numpy as np
from timeframe_config import get_timeframe_config
from api_data_fetcher import fetch_marketing_hourly, fetch_google_spend, fetch_shopify_sales_orders_detail
from revenue_gst import apply_net_revenue, apply_net_revenue_column

# Preferred funnel-based column order
FUNNEL_ORDER: list[str] = [
    # Meta delivery
    'date_start', 'channel', 'campaign_name', 'adset_name', 'ad_name',
    'net_profit',
    'clicks',
    'ctr',
    # Landing engagement
    'bounce_rate',
    # Consideration
    '_add_to_cart', '_initiate_checkout',
    # Conversion
    'shopify_orders',
    # Financials
    'spend', 'shopify_revenue', 'shopify_cogs', 'gross_roas', 'net_roas', 'profit_margin',
    # SKU detail
    'sku', 'vendor', 'product_title', 'variant_title', 'quantity',
    'unit_price', 'unit_cost', 'sku_revenue', 'sku_cogs',
]

SKU_FIELDS: list[str] = [
    'sku', 'vendor', 'product_title', 'variant_title', 'quantity',
    'unit_price', 'unit_cost', 'sku_revenue', 'sku_cogs',
]

def order_columns_by_funnel(df: pd.DataFrame, include_sku: bool = False) -> list[str]:
    """
    Return an ordered list of columns for df following FUNNEL_ORDER.
    If include_sku=False, SKU fields are excluded from the preferred list.
    """
    preferred = FUNNEL_ORDER if include_sku else [c for c in FUNNEL_ORDER if c not in SKU_FIELDS]
    present_preferred = [c for c in preferred if c in df.columns]
    remainder = [c for c in df.columns if c not in present_preferred]
    return present_preferred + remainder


def drop_internal_id_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Remove internal ID fields from presentation/export sheets.
    Keep these columns in intermediate dataframes for joins/enrichment only.
    """
    if df is None or df.empty:
        return df
    id_cols = [
        "campaign_id",
        "adset_id",
        "ad_id",
        "entity_id",
        "order_item_id",
        "product_id",
        "variant_id",
        "item_id",
    ]
    present = [c for c in id_cols if c in df.columns]
    return df.drop(columns=present) if present else df


def _meta_attributed_orders_total(start_date: str, end_date: str) -> int:
    """
    Return the exact Meta order total used by the channel performance section.
    This keeps the Meta funnel's final "Orders" stage aligned with the PDF
    channel table even when /v1/meta-funnel reports a narrower session-based
    order count.
    """
    try:
        from api_data_fetcher import fetch_historical_dashboard
        from metric_calculators import channel_metrics_from_historical_dashboard

        data = fetch_historical_dashboard(start_date, end_date)
        if not data:
            return 0
        metrics = channel_metrics_from_historical_dashboard(data)
        return int((metrics.get('meta') or {}).get('order_count', 0) or 0)
    except Exception:
        return 0

# Columns to round to 2 decimals for presentation
RATE_COLS: list[str] = [
    'ctr', 'bounce_rate', 'gross_roas', 'net_roas', 'profit_margin', 'conversion_rate', 'be_roas'
]
MONEY_COLS: list[str] = [
    'spend', 'shopify_revenue', 'shopify_cogs', 'net_profit',
    'unit_price', 'unit_cost', 'sku_revenue', 'sku_cogs'
]

def round_for_output(df: pd.DataFrame) -> pd.DataFrame:
    """
    Return a copy of df with key rate and monetary columns rounded to 2 decimals
    when present. Non-present columns are ignored. Counts remain unchanged.
    """
    if df.empty:
        return df
    out = df.copy()
    for col in RATE_COLS + MONEY_COLS:
        if col in out.columns:
            series = pd.to_numeric(out[col], errors='coerce')
            # Replace +/-inf from accidental divide-by-zero with 0
            try:
                import numpy as _np
                series = series.replace([_np.inf, -_np.inf], 0)
            except Exception:
                series = series.replace([float('inf'), float('-inf')], 0)
            # Keep NaN for net_roas so non-campaign rows show blank in Excel
            if col == 'net_roas':
                out[col] = series.round(2)
            else:
                out[col] = series.fillna(0).round(2)
    return out

def _append_spend_to_google_rows(df: pd.DataFrame, total_spend: float) -> pd.DataFrame:
    """
    Group all Google spend into a single total spend value.
    Replaces all Google spend values with the total from the API.
    """
    try:
        if df is None or df.empty or total_spend is None or total_spend <= 0:
            return df
        if 'channel' not in df.columns or 'spend' not in df.columns:
            return df
        # Case-insensitive match on channel name
        mask = df['channel'].astype(str).str.strip().str.casefold().eq('google')
        if not mask.any():
            try:
                print(f"[GoogleAds] No channel=='Google' rows present in target df; channels present: {sorted(df['channel'].dropna().astype(str).str.strip().str.casefold().unique().tolist())}")
            except Exception:
                print("[GoogleAds] No channel=='Google' rows present in target df; unable to list channels")
            return df
        
        # Get the sum of existing Google spend
        existing_spend = df.loc[mask, 'spend'].sum()
        
        # Replace all Google spend with the total from API
        df.loc[mask, 'spend'] = float(total_spend)
        
        try:
            print(f"[GoogleAds] Replaced Google spend: existing={round(existing_spend, 2)} -> API total={round(float(total_spend), 2)}")
        except Exception:
            pass
        return df
    except Exception:
        return df


def _normalize_product_details_list(product_details: str | dict | list | None) -> list[dict]:
    """Parse product_details into a list of order/line dicts."""
    if product_details is None:
        return []
    try:
        if isinstance(product_details, list):
            data = product_details
        elif isinstance(product_details, dict):
            if 'skus' in product_details and 'summary' in product_details:
                data = [product_details]
            else:
                data = [product_details]
        else:
            s = str(product_details).strip()
            try:
                data = json.loads(s)
            except Exception:
                s2 = s.encode('utf-8').decode('unicode_escape')
                data = json.loads(s2)
            if not isinstance(data, list):
                data = [data]
        return [item for item in data if isinstance(item, dict)]
    except Exception:
        return []


def _merge_product_details_orders(product_details_values) -> list[dict]:
    """Merge hourly product_details payloads, keeping one copy of each order_id."""
    by_order_id: dict[str, dict] = {}
    anon_idx = 0
    for product_details in product_details_values:
        for order in _normalize_product_details_list(product_details):
            order_id = order.get('order_id') or order.get('order_name')
            key = str(order_id) if order_id else f'__anon_{anon_idx}'
            if not order_id:
                anon_idx += 1
            by_order_id[key] = order
    return list(by_order_id.values())


def parse_product_details(product_details: str | dict | list, attributed_revenue: float = 0.0, attributed_cogs: float = 0.0, attributed_quantity: int = 0) -> list[dict]:
    """
    Parse the product_details and return a list of SKU dicts with essential fields.
    
    Two modes of operation:
    1. Attribution mode (attributed_revenue/cogs/quantity > 0): 
       - Used for Meta Ads & Organic
       - Uses attributed_orders_* columns as source of truth
       - product_details for SKU codes and metadata
       - If unit_price and unit_cost are present in product_details:
         * Uses them directly to calculate sku_revenue and sku_cogs
       - If unit_price and unit_cost are NOT present:
         * Distributes revenue/COGS across SKUs using quantity weighting
    
    2. Direct mode (attributed_revenue/cogs/quantity = 0):
       - Used for Google Ads
       - Uses revenue/COGS values directly from product_details JSONB
       - Each SKU object contains its own sku_revenue, sku_cogs, unit_price, unit_cost
    
    Args:
        product_details: JSONB column containing SKU data
        attributed_revenue: Total attributed revenue (0 = use product_details directly)
        attributed_cogs: Total attributed COGS (0 = use product_details directly)
        attributed_quantity: Total attributed quantity (0 = use product_details directly)
    
    Returns:
        List of SKU dictionaries with financial data from appropriate source
    """
    if product_details is None:
        return []
    
    # Determine mode: attribution mode or direct mode
    use_attribution_mode = attributed_revenue > 0 or attributed_cogs > 0 or attributed_quantity > 0
    
    try:
        # Handle different input types
        if isinstance(product_details, list):
            data = product_details
        elif isinstance(product_details, dict):
            # Check if this is the new standard format (direct object with skus and summary)
            if 'skus' in product_details and 'summary' in product_details:
                data = [product_details]  # Wrap in list for consistent processing
            else:
                data = [product_details]
        else:
            s = str(product_details).strip()
            # Try to parse as JSON
            try:
                data = json.loads(s)
            except Exception:
                # Try un-escaping once and re-parse
                s2 = s.encode('utf-8').decode('unicode_escape')
                data = json.loads(s2)
            if not isinstance(data, list):
                data = [data]
        
        cleaned = []
        
        # Flat Shopify line-item list (organic channel-attribution items[])
        if (
            isinstance(data, list)
            and data
            and isinstance(data[0], dict)
            and data[0].get("sku")
            and "skus" not in data[0]
            and "order_id" not in data[0]
        ):
            sku_metadata = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                sku_code = item.get("sku") or item.get("seller_sku")
                if not sku_code:
                    continue
                sku_metadata.append({
                    "sku": str(sku_code),
                    "vendor": str(item.get("vendor", "Unknown")),
                    "quantity": int(item.get("quantity") or item.get("quantity_ordered") or 1),
                    "product_title": str(item.get("product_title") or item.get("product_name") or item.get("title") or item.get("name") or "Unknown Product"),
                    "variant_title": str(item.get("variant_title") or item.get("name") or sku_code),
                })
                if item.get("unit_price") is not None:
                    sku_metadata[-1]["unit_price"] = float(item.get("unit_price") or item.get("price") or 0)
                if item.get("unit_cost") is not None or item.get("cogs") is not None:
                    sku_metadata[-1]["unit_cost"] = float(item.get("unit_cost") or item.get("cogs") or item.get("net_cogs") or 0)
            if sku_metadata:
                data = [{"skus": sku_metadata}] if use_attribution_mode else data
        
        if use_attribution_mode:
            # ATTRIBUTION MODE: Extract metadata, distribute attributed values
            sku_metadata = []
            for order in data:
                if not isinstance(order, dict):
                    continue

                order_handled = False

                # Check if this is standard format (has 'skus' array)
                if 'skus' in order and isinstance(order['skus'], list) and len(order['skus']) > 0:
                    first_sku = order['skus'][0] if order['skus'] else None
                    
                    if isinstance(first_sku, dict) and first_sku.get('sku'):
                        # Standard format: skus array contains full SKU objects with metadata
                        for sku_obj in order['skus']:
                            if isinstance(sku_obj, dict) and sku_obj.get('sku'):
                                sku_meta = {
                                    'sku': str(sku_obj.get('sku', '')),
                                    'vendor': str(sku_obj.get('vendor', 'Unknown')),
                                    'quantity': int(sku_obj.get('quantity', 1)),
                                    'product_title': str(sku_obj.get('product_title', 'Unknown Product')),
                                    'variant_title': str(sku_obj.get('variant_title', 'Unknown Variant')),
                                }
                                # Preserve unit_price and unit_cost if available
                                if 'unit_price' in sku_obj:
                                    sku_meta['unit_price'] = float(sku_obj.get('unit_price', 0) or 0)
                                if 'unit_cost' in sku_obj:
                                    sku_meta['unit_cost'] = float(sku_obj.get('unit_cost', 0) or 0)
                                sku_metadata.append(sku_meta)
                        order_handled = True
                    elif isinstance(first_sku, str):
                        # Legacy format: skus array contains SKU strings only
                        for sku_code in order['skus']:
                            if sku_code:  # Skip None/empty SKUs
                                sku_metadata.append({
                                    'sku': str(sku_code),
                                    'vendor': 'Unknown',
                                    'quantity': 1,
                                    'product_title': f'Product {sku_code}',
                                    'variant_title': str(sku_code),
                                })
                        order_handled = True

                # v1 attribution API: order objects carry Shopify line_items, not skus[]
                if not order_handled:
                    line_items = order.get("line_items") or order.get("items") or []
                    if line_items:
                        for item in line_items:
                            if not isinstance(item, dict):
                                continue
                            sku_code = item.get("sku") or item.get("seller_sku")
                            if not sku_code:
                                continue
                            qty = int(item.get("quantity") or item.get("quantity_ordered") or 1)
                            sku_metadata.append({
                                'sku': str(sku_code),
                                'vendor': str(item.get('vendor', 'Unknown')),
                                'quantity': qty if qty > 0 else 1,
                                'product_title': str(
                                    item.get('product_title')
                                    or item.get('title')
                                    or item.get('name')
                                    or 'Unknown Product'
                                ),
                                'variant_title': str(
                                    item.get('variant_title') or item.get('name') or sku_code
                                ),
                            })
                        order_handled = True

                if not order_handled:
                    # No SKUs in product_details - extract from legacy format if available
                    skus_list = order.get('skus', [])
                    if skus_list and skus_list != [None]:
                        for sku_code in skus_list:
                            if sku_code:
                                sku_metadata.append({
                                    'sku': str(sku_code),
                                    'vendor': 'Unknown',
                                    'quantity': 1,
                                    'product_title': f'Product {sku_code}',
                                    'variant_title': str(sku_code),
                                })
            
            # If no SKU metadata found, create a placeholder
            if not sku_metadata:
                sku_metadata = [{
                    'sku': 'Unknown',
                    'vendor': 'Unknown',
                    'quantity': attributed_quantity if attributed_quantity > 0 else 1,
                    'product_title': 'Unknown Product',
                    'variant_title': 'Unknown Variant',
                }]
            
            # Process SKUs - use unit prices if available, otherwise distribute
            total_sku_quantity = sum(s['quantity'] for s in sku_metadata)
            
            for sku_meta in sku_metadata:
                sku_quantity = sku_meta['quantity']
                
                # Check if unit_price and unit_cost are already provided
                if 'unit_price' in sku_meta and 'unit_cost' in sku_meta:
                    # Use unit prices directly and calculate totals (GST net on revenue only)
                    unit_price = sku_meta['unit_price']
                    unit_cost = sku_meta['unit_cost']
                    sku_revenue = unit_price * sku_quantity
                    sku_cogs = unit_cost * sku_quantity
                    sku_revenue = apply_net_revenue(sku_revenue)
                    unit_price = sku_revenue / sku_quantity if sku_quantity > 0 else apply_net_revenue(float(unit_price or 0))
                else:
                    # Distribute attributed revenue and COGS across SKUs
                    if total_sku_quantity > 0:
                        share = sku_quantity / total_sku_quantity
                    else:
                        share = 1.0 / len(sku_metadata)
                    
                    sku_revenue = attributed_revenue * share
                    sku_cogs = attributed_cogs * share
                    unit_price = sku_revenue / sku_quantity if sku_quantity > 0 else 0
                    unit_cost = sku_cogs / sku_quantity if sku_quantity > 0 else 0
                
                cleaned.append({
                    'sku': sku_meta['sku'],
                    'vendor': sku_meta['vendor'],
                    'quantity': sku_quantity,
                    'sku_cogs': sku_cogs,
                    'unit_cost': unit_cost,
                    'unit_price': unit_price,
                    'sku_revenue': sku_revenue,
                    'product_title': sku_meta['product_title'],
                    'variant_title': sku_meta['variant_title'],
                })
        else:
            # DIRECT MODE: Use revenue/COGS values from product_details directly
            for order in data:
                if not isinstance(order, dict):
                    continue

                # Single line-item dict (flat list from some API payloads)
                if order.get("sku") and not order.get("line_items") and not order.get("items") and not order.get("skus"):
                    item = order
                    sku_code = item.get("sku") or item.get("seller_sku")
                    if sku_code:
                        qty = int(item.get("quantity") or item.get("quantity_ordered") or 1)
                        qty_safe = qty if qty > 0 else 1
                        up_gross = float(item.get("unit_price") or item.get("price") or 0)
                        sr_gross = float(item.get("sku_revenue") or item.get("line_value") or item.get("total") or 0)
                        base_rev = sr_gross if sr_gross else up_gross * qty_safe
                        sr = apply_net_revenue(base_rev)
                        up = sr / qty_safe if qty_safe else apply_net_revenue(up_gross)
                        cleaned.append({
                            "sku": str(sku_code),
                            "vendor": str(item.get("vendor", "Unknown")),
                            "quantity": qty if qty > 0 else 1,
                            "sku_cogs": float(item.get("sku_cogs") or item.get("cogs") or item.get("net_cogs") or 0),
                            "unit_cost": float(item.get("unit_cost") or item.get("cogs") or item.get("net_cogs") or 0),
                            "unit_price": up,
                            "sku_revenue": sr,
                            "product_title": str(item.get("product_title") or item.get("product_name") or item.get("title") or "Unknown Product"),
                            "variant_title": str(item.get("variant_title") or item.get("name") or sku_code),
                        })
                    continue

                line_items = order.get("line_items") or order.get("items") or []
                if line_items:
                    for item in line_items:
                        if not isinstance(item, dict):
                            continue
                        sku_code = item.get("sku") or item.get("seller_sku")
                        if not sku_code:
                            continue
                        qty = int(item.get("quantity") or item.get("quantity_ordered") or 1)
                        qty_safe = qty if qty > 0 else 1
                        up_gross = float(item.get("unit_price") or item.get("price") or 0)
                        sr_gross = float(
                            item.get("sku_revenue")
                            or item.get("line_value")
                            or item.get("total")
                            or item.get("item_revenue")
                            or 0
                        )
                        base_rev = sr_gross if sr_gross else up_gross * qty_safe
                        sr = apply_net_revenue(base_rev)
                        up = sr / qty_safe if qty_safe else apply_net_revenue(up_gross)
                        cleaned.append({
                            "sku": str(sku_code),
                            "vendor": str(item.get("vendor", "Unknown")),
                            "quantity": qty if qty > 0 else 1,
                            "sku_cogs": float(item.get("sku_cogs") or item.get("cogs") or item.get("net_cogs") or 0),
                            "unit_cost": float(item.get("unit_cost") or item.get("cogs") or item.get("net_cogs") or 0),
                            "unit_price": up,
                            "sku_revenue": sr,
                            "product_title": str(item.get("product_title") or item.get("title") or item.get("name") or "Unknown Product"),
                            "variant_title": str(item.get("variant_title") or item.get("name") or sku_code),
                        })
                    continue
                
                # Check if this is standard format (has 'skus' array with full SKU objects)
                if 'skus' in order and isinstance(order['skus'], list) and len(order['skus']) > 0:
                    first_sku = order['skus'][0] if order['skus'] else None
                    
                    if isinstance(first_sku, dict) and first_sku.get('sku'):
                        # Standard format: skus array contains full SKU objects with revenue/COGS
                        skus_list = order['skus']
                        for sku_obj in skus_list:
                            if isinstance(sku_obj, dict) and sku_obj.get('sku'):
                                sku_code = str(sku_obj.get('sku', ''))
                                qty = int(sku_obj.get('quantity', 1))
                                qty_safe = qty if qty > 0 else 1
                                up_gross = float(sku_obj.get('unit_price', 0) or 0)
                                sr_gross = float(sku_obj.get('sku_revenue', 0) or 0)
                                base_rev = sr_gross if sr_gross else up_gross * qty_safe
                                sr = apply_net_revenue(base_rev)
                                up = sr / qty_safe if qty_safe else apply_net_revenue(up_gross)
                                cleaned.append({
                                    'sku': sku_code,
                                    'vendor': str(sku_obj.get('vendor', 'Unknown')),
                                    'quantity': qty if qty > 0 else 1,
                                    'sku_cogs': float(sku_obj.get('sku_cogs', 0) or 0),
                                    'unit_cost': float(sku_obj.get('unit_cost', 0) or 0),
                                    'unit_price': up,
                                    'sku_revenue': sr,
                                    'product_title': str(sku_obj.get('product_title', 'Unknown Product')),
                                    'variant_title': str(sku_obj.get('variant_title', 'Unknown Variant')),
                                })
                        continue
                    elif isinstance(first_sku, str):
                        # Legacy format: skus array contains SKU strings, with order-level totals
                        order_cogs = float(order.get('total_cogs', 0) or 0)
                        order_value = apply_net_revenue(float(order.get('order_value', 0) or 0))
                        skus_list = order['skus']
                        
                        # Distribute order values across SKUs
                        num_skus = len(skus_list)
                        sku_cogs = order_cogs / num_skus if num_skus > 0 else 0
                        sku_revenue = order_value / num_skus if num_skus > 0 else 0
                        
                        for sku_code in skus_list:
                            if sku_code:
                                cleaned.append({
                                    'sku': str(sku_code),
                                    'vendor': 'Unknown',
                                    'quantity': 1,
                                    'sku_cogs': sku_cogs,
                                    'unit_cost': sku_cogs,
                                    'unit_price': sku_revenue,
                                    'sku_revenue': sku_revenue,
                                    'product_title': f'Product {sku_code}',
                                    'variant_title': str(sku_code),
                                })
                        continue
                
                # Legacy Meta format: order has total_cogs, order_value, and skus as string list
                order_cogs = float(order.get('total_cogs', 0) or 0)
                order_value = apply_net_revenue(float(order.get('order_value', 0) or 0))
                skus_list = order.get('skus', [])
                
                if not skus_list or skus_list == [None]:
                    cleaned.append({
                        'sku': 'Unknown',
                        'vendor': 'Unknown',
                        'quantity': 1,
                        'sku_cogs': order_cogs,
                        'unit_cost': order_cogs,
                        'unit_price': order_value,
                        'sku_revenue': order_value,
                        'product_title': 'Unknown Product',
                        'variant_title': 'Unknown Variant',
                    })
                else:
                    num_skus = len(skus_list)
                    sku_cogs = order_cogs / num_skus if num_skus > 0 else 0
                    sku_revenue = order_value / num_skus if num_skus > 0 else 0
                    
                    for sku_code in skus_list:
                        if sku_code:
                            cleaned.append({
                                'sku': str(sku_code),
                                'vendor': 'Unknown',
                                'quantity': 1,
                                'sku_cogs': sku_cogs,
                                'unit_cost': sku_cogs,
                                'unit_price': sku_revenue,
                                'sku_revenue': sku_revenue,
                                'product_title': f'Product {sku_code}',
                                'variant_title': str(sku_code),
                            })
        
        return cleaned
    except Exception as e:
        print(f"Error parsing product_details: {e}")
        return []

def explode_skus(df: pd.DataFrame) -> pd.DataFrame:
    """
    Create a SKU-level table by exploding product_details.
    
    Data source handling:
    - Google Ads: Uses product_details column directly (contains accurate SKU-level data)
    - Meta Ads & Organic: Uses attributed_orders_* columns as source of truth, 
      product_details only for SKU codes and metadata
    
    Includes mapping keys to identify the parent ad row.
    """
    if df.empty or 'product_details' not in df.columns:
        return pd.DataFrame()

    df = df.copy()
    data_source = None
    if not df.empty and 'source' in df.columns:
        data_source = df['source'].iloc[0] if len(df) > 0 else None
    
    # Check for required attribution columns (only needed for Meta/Organic)
    if data_source in ['Meta Ads', 'Organic']:
        required_cols = ['attributed_orders_revenue', 'attributed_orders_cogs', 'attributed_orders_quantity']
        missing_cols = [c for c in required_cols if c not in df.columns]
        if missing_cols:
            print(f"[SKU Explosion] Warning: Missing attribution columns for {data_source}: {missing_cols}")

    sku_rows = []
    # Define ID columns based on data source
    if data_source == 'Organic':
        # Organic data doesn't have adset_name, ad_name
        id_cols = ['date_start','hour','channel','campaign_name']
        ad_group_cols = ['date_start','channel','campaign_name']
    else:
        # Meta/Google data has full hierarchy
        id_cols = ['date_start','hour','channel','campaign_name','adset_name','ad_name']
        ad_group_cols = ['date_start','channel','campaign_name','adset_name','ad_name']
    
    id_cols = [c for c in id_cols if c in df.columns]
    ad_group_cols = [c for c in ad_group_cols if c in df.columns]

    if data_source == 'Google Ads' and ad_group_cols:
        # Google: collapse to campaign/day and dedupe orders (direct-mode parse).
        if 'attributed_orders_count' in df.columns:
            df['attributed_orders_count'] = pd.to_numeric(
                df['attributed_orders_count'], errors='coerce'
            ).fillna(0)
        attr_df = df[df.get('attributed_orders_count', 0) > 0].copy()
        if attr_df.empty:
            return pd.DataFrame()

        google_group_cols = [c for c in ['date_start', 'channel', 'campaign_name'] if c in df.columns]
        for _, group in attr_df.groupby(google_group_cols, dropna=False):
            merged_orders = _merge_product_details_orders(group['product_details'])
            skus = parse_product_details(merged_orders, 0.0, 0.0, 0)
            if not skus:
                continue
            base = {c: group.iloc[0][c] for c in google_group_cols}
            for s in skus:
                sku_rows.append({**base, **s})
        return pd.DataFrame(sku_rows) if sku_rows else pd.DataFrame()

    if data_source in ['Meta Ads', 'Organic'] and ad_group_cols:
        # Collapse hourly rows to ad/day grain: the API repeats the day's orders in
        # product_details on every hour row, so row-wise parsing over-counts SKUs.
        numeric_attr = {
            'attributed_orders_revenue': 0.0,
            'attributed_orders_cogs': 0.0,
            'attributed_orders_quantity': 0,
            'attributed_orders_count': 0,
        }
        for col, default in numeric_attr.items():
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(default)

        has_attr = (
            (df.get('attributed_orders_count', 0) > 0)
            | (df.get('attributed_orders_quantity', 0) > 0)
            | (df.get('attributed_orders_revenue', 0) > 0)
        )
        attr_df = df[has_attr].copy()
        if attr_df.empty:
            return pd.DataFrame()

        for _, group in attr_df.groupby(ad_group_cols, dropna=False):
            attributed_revenue = apply_net_revenue(float(group['attributed_orders_revenue'].sum()))
            attributed_cogs = float(group['attributed_orders_cogs'].sum())
            attributed_quantity = int(group['attributed_orders_quantity'].sum())
            merged_orders = _merge_product_details_orders(group['product_details'])
            skus = parse_product_details(
                merged_orders,
                attributed_revenue=attributed_revenue,
                attributed_cogs=attributed_cogs,
                attributed_quantity=attributed_quantity,
            )
            if not skus:
                continue
            base = {c: group.iloc[0][c] for c in ad_group_cols}
            for s in skus:
                sku_rows.append({**base, **s})
        return pd.DataFrame(sku_rows) if sku_rows else pd.DataFrame()

    for idx, row in df.iterrows():
        product_details = row.get('product_details')
        source = row.get('source', 'Unknown')
        
        # Determine whether to use attribution columns or product_details
        use_attribution_columns = source in ['Meta Ads', 'Organic']
        
        if use_attribution_columns:
            # Meta and Organic: Use attributed_orders_* columns as source of truth
            attributed_revenue = apply_net_revenue(float(row.get('attributed_orders_revenue', 0) or 0))
            attributed_cogs = float(row.get('attributed_orders_cogs', 0) or 0)
            attributed_quantity = int(row.get('attributed_orders_quantity', 0) or 0)
            attributed_orders = int(row.get('attributed_orders_count', 0) or 0)
            # Hourly rows often carry the day's product_details even when that hour
            # has zero attributed orders. Parsing those rows in direct mode duplicates
            # every SKU once per hour and inflates quantity (e.g. 5 units -> 55).
            if attributed_orders == 0 and attributed_quantity == 0 and attributed_revenue == 0:
                continue
        else:
            # Google: Use product_details directly (pass 0 to use values from product_details)
            attributed_revenue = 0.0
            attributed_cogs = 0.0
            attributed_quantity = 0
        
        # Parse product_details with appropriate source
        skus = parse_product_details(
            product_details, 
            attributed_revenue=attributed_revenue,
            attributed_cogs=attributed_cogs,
            attributed_quantity=attributed_quantity
        )
        
        if not skus:
            continue
        
        base = {c: row.get(c) for c in id_cols}
        for s in skus:
            rec = {**base, **s}
            sku_rows.append(rec)

    if not sku_rows:
        return pd.DataFrame()
    
    return pd.DataFrame(sku_rows)

def build_ad_sku_rollup(df: pd.DataFrame) -> pd.DataFrame:
    """
    Build an aggregated SKU-level rollup at ad level.
    One row per (date_start, channel, campaign_name, adset_name, ad_name, sku).
    Sums quantities/revenue/cogs/profit across duplicates.
    """
    exploded = explode_skus(df)
    if exploded.empty:
        return pd.DataFrame()

    # Ensure numeric columns are numeric
    for col in ['quantity', 'sku_cogs', 'unit_cost', 'unit_price', 'sku_revenue']:
        if col in exploded.columns:
            exploded[col] = pd.to_numeric(exploded[col], errors='coerce').fillna(0)

    # Determine grouping columns based on data source
    if not exploded.empty and 'source' in exploded.columns:
        source = exploded['source'].iloc[0] if len(exploded) > 0 else ''
        if source == 'Organic':
            # Organic data doesn't have adset_name, ad_name
            group_cols = ['date_start', 'channel', 'campaign_name', 'sku']
            string_cols = ['date_start', 'channel', 'campaign_name', 'sku']
        else:
            # Meta/Google data has full hierarchy
            group_cols = ['date_start', 'channel', 'campaign_name', 'adset_name', 'ad_name', 'sku']
            string_cols = ['date_start', 'channel', 'campaign_name', 'adset_name', 'ad_name', 'sku']
    else:
        # Fallback to full hierarchy
        group_cols = ['date_start', 'channel', 'campaign_name', 'adset_name', 'ad_name', 'sku']
        string_cols = ['date_start', 'channel', 'campaign_name', 'adset_name', 'ad_name', 'sku']
    
    # Ensure all grouping columns are strings to avoid unhashable type errors
    for col in string_cols:
        if col in exploded.columns:
            exploded[col] = exploded[col].astype(str).fillna('')

    # Filter group_cols to only include columns that exist in the DataFrame
    group_cols = [c for c in group_cols if c in exploded.columns]
    
    # Ensure all grouping columns contain only hashable values (convert to string if needed)
    for col in group_cols:
        if col in exploded.columns:
            # Convert to string to ensure hashability
            exploded[col] = exploded[col].astype(str)

    agg_dict = {
        'quantity': 'sum',
        'sku_revenue': 'sum',
        'sku_cogs': 'sum',
        # Keep one non-null representative value for attributes
        'vendor': 'first',
        'product_title': 'first',
        'variant_title': 'first',
    }
    present_agg = {k: v for k, v in agg_dict.items() if k in exploded.columns}

    rollup = exploded.groupby(group_cols, dropna=False).agg(present_agg).reset_index()

    # Derive unit price/cost from totals where quantity > 0
    if 'quantity' in rollup.columns:
        qty = rollup['quantity'].replace(0, pd.NA)
        if 'sku_revenue' in rollup.columns:
            rollup['unit_price'] = (rollup['sku_revenue'] / qty).fillna(0)
        if 'sku_cogs' in rollup.columns:
            rollup['unit_cost'] = (rollup['sku_cogs'] / qty).fillna(0)

    # Order columns
    ordered_cols = [
        'date_start', 'channel', 'campaign_name', 'adset_name', 'ad_name',
        'sku', 'vendor', 'product_title', 'variant_title',
        'quantity', 'unit_price', 'unit_cost', 'sku_revenue', 'sku_cogs'
    ]
    cols = [c for c in ordered_cols if c in rollup.columns] + [c for c in rollup.columns if c not in ordered_cols]
    sort_cols = [c for c in ['date_start', 'channel', 'campaign_name', 'adset_name', 'ad_name', 'sku'] if c in rollup.columns]
    
    final_result = rollup[cols]
    if sort_cols:
        final_result = final_result.sort_values(sort_cols).reset_index(drop=True)
    
    return final_result

def transform_attribution_data(df: pd.DataFrame) -> pd.DataFrame:
    """
    Transform attribution data from fetch_marketing_hourly to match expected structure.
    Maps column names and ensures proper data types.
    """
    if df.empty:
        return df
    
    # Create a copy to avoid modifying original
    transformed = df.copy()
    
    # Map column names to match expected structure
    column_mapping = {
        'spend_cost': 'spend',
        'attributed_orders_count': 'shopify_orders',
        'attributed_orders_revenue': 'shopify_revenue',
        'attributed_orders_cogs': 'shopify_cogs',
        'attributed_orders_quantity': 'total_sku_quantity',
        # Map funnel action columns to expected names
        'action_landing_page_view': '_landing_page_view',
        'action_onsite_web_initiate_checkout': '_initiate_checkout',
        'action_onsite_web_add_to_cart': '_add_to_cart'
    }
    
    for old_col, new_col in column_mapping.items():
        if old_col in transformed.columns:
            transformed[new_col] = transformed[old_col]
    
    # Ensure numeric columns are properly typed
    numeric_cols = [
        'impressions', 'clicks', 'spend', 'cpm', 'cpc', 'ctr',
        'shopify_orders', 'shopify_revenue', 'shopify_cogs', 'total_sku_quantity',
        'attributed_orders_revenue',
        'action_onsite_web_view_content', 'action_onsite_web_add_to_cart', 'action_onsite_web_initiate_checkout',
        'action_offsite_pixel_view_content', 'action_offsite_pixel_add_to_cart', 'action_offsite_pixel_initiate_checkout',
        'action_landing_page_view',
        # Include mapped column names
        '_landing_page_view', '_initiate_checkout', '_add_to_cart'
    ]
    
    for col in numeric_cols:
        if col in transformed.columns:
            transformed[col] = pd.to_numeric(transformed[col], errors='coerce').fillna(0)

    # NOTE: revenue from the v1 attribution API is ALREADY ex-GST (the backend's
    # net_sales / attributed_orders_revenue; the tax-inclusive value is a
    # separate field). Applying apply_net_revenue_column (÷1.18) here double-nets
    # GST and understates revenue ~18%, so meta_ads_rollup no longer reconciled
    # with the dashboard/email channel cards (e.g. 51,902.55 vs the correct
    # 61,245.01). Left un-adjusted so it matches /v1/pnl/summary and the KPI strip.
    # (Was: apply_net_revenue_column on attributed_orders_revenue / shopify_revenue.)

    return transformed


def _fetch_meta_funnel_by_ad(start_date: str, end_date: str) -> tuple[dict, dict]:
    """Real on-site funnel metrics per ad_id from GET /v1/meta-funnel.

    Returns ({ad_id: {sessions, landing_page_views, add_to_cart, checkout_start,
    bounce_rate, conversion_rate}}, summary). The attribution API carries no
    landing-page/product-view counts, so the old (clicks - LPV)/clicks bounce
    rate collapsed to a fake 100; this sources the canonical session-funnel
    numbers instead (docs/data_sources_cannonical.md §3).
    """
    try:
        from entity_report import _meta_funnel_maps
        return _meta_funnel_maps(start_date, end_date)
    except Exception as e:
        print(f"[Meta Funnel] fetch failed; keeping attribution-derived funnel: {e}")
        return {}, {}


def build_channel_summary_from_marketing_df(df: pd.DataFrame) -> dict:
    """Channel-performance buckets (meta/google/organic) from attribution hourly data.

    Used for entity-report Excel rollups and optional legacy PDF overlay when
    CHANNEL_FROM_ATTRIBUTION=true. The daily/WTD/MTD PDF and email KPI tables
    use GET /v1/historical/dashboard instead (see get_organized_metrics_for_pdf).
    """
    from metric_calculators import enrich_channel_bucket

    empty = enrich_channel_bucket(0, 0, 0, 0)
    if df is None or df.empty:
        return {"meta": dict(empty), "google": dict(empty), "organic": dict(empty)}

    t = transform_attribution_data(df)

    def _sum(frame, col):
        return float(pd.to_numeric(frame[col], errors="coerce").fillna(0).sum()) if col in frame.columns else 0.0

    out = {}
    for key, src in (("meta", "Meta Ads"), ("google", "Google Ads"), ("organic", "Organic")):
        sub = t[t.get("source") == src]
        out[key] = enrich_channel_bucket(
            sales=_sum(sub, "shopify_revenue"),
            ad_spend=(_sum(sub, "spend") if key != "organic" else 0.0),
            cogs=_sum(sub, "shopify_cogs"),
            order_count=int(_sum(sub, "shopify_orders")),
        )
    return out


def build_meta_ads_rollup_with_sku(df: pd.DataFrame) -> pd.DataFrame:
    """
    Combine ad-level performance metrics with SKU-level attribution so that
    each row represents an ad and a single SKU attributed to it. Duplicate
    ad+sku rows are aggregated via build_ad_sku_rollup.
    """
    # Get ad-level metrics
    metrics = build_meta_ads_rollup(df)
    # Get SKU-level rollup
    sku_rollup = build_ad_sku_rollup(df)

    # Normalize key columns to ensure grouping/merging works even with stray spaces/case
    key_cols = ['date_start','channel','campaign_name','adset_name','ad_name','sku']
    for _df in [metrics, sku_rollup]:
        for c in key_cols:
            if c in _df.columns:
                _df[c] = _df[c].astype(str).str.strip()

    if metrics.empty:
        return pd.DataFrame()

    merge_keys = [
        'date_start','channel','campaign_name','adset_name','ad_name'
    ]
    merge_keys = [k for k in merge_keys if k in metrics.columns and k in sku_rollup.columns]

    if sku_rollup.empty:
        # Ensure SKU columns exist even if empty
        for col in ['sku','vendor','product_title','variant_title','quantity','unit_price','unit_cost','sku_revenue','sku_cogs']:
            metrics[col] = pd.NA
        return metrics

    out = metrics.merge(sku_rollup, on=merge_keys, how='left')

    # Keep ALL ads in the rollup, even those without SKU data
    # Fill NaN SKUs with placeholder values instead of filtering them out
    if 'sku' in out.columns:
        before_fill = len(out)
        out = out.fillna({
            'sku': '',
            'vendor': 'Unknown',
            'product_title': 'Unknown Product',
            'variant_title': 'Unknown Variant',
            'quantity': 0,
            'unit_price': 0,
            'unit_cost': 0,
            'sku_revenue': 0,
            'sku_cogs': 0
        })
        after_fill = len(out)

    # If duplicates exist for same ad+sku, merge them by summing numeric fields
    group_keys = merge_keys + (['sku'] if 'sku' in out.columns else [])
    group_keys = [k for k in group_keys if k in out.columns]

    if group_keys:
        # Metrics to carry over (identical per ad) – take first to avoid double counting
        metrics_first = [
            'impressions','clicks','_landing_page_view','_add_to_cart','_initiate_checkout',
            'shopify_orders','total_sku_quantity','shopify_revenue','shopify_cogs','spend',
            'ad_id','bounce_rate'
        ]
        # No summation for ad-level spend/revenue/cogs to avoid double counting across SKU rows
        revenue_sum = []
        # SKU numeric fields to sum when duplicates present
        sku_sum = ['quantity','sku_revenue','sku_cogs']
        # Non-key descriptors
        first_cols = ['vendor','product_title','variant_title']

        present_first_metrics = {c:'first' for c in metrics_first if c in out.columns}
        present_sum_sku = {c:'sum' for c in sku_sum if c in out.columns}
        present_sum_revenue = {c:'sum' for c in revenue_sum if c in out.columns}
        present_first_desc = {c:'first' for c in first_cols if c in out.columns and c not in group_keys}
        agg_map = {**present_first_metrics, **present_sum_sku, **present_sum_revenue, **present_first_desc}
        # carry-over columns not explicitly aggregated -> first
        for c in out.columns:
            if c in group_keys or c in agg_map:
                continue
            agg_map[c] = 'first'
        out = out.groupby(group_keys, dropna=False).agg(agg_map).reset_index()

        # Recompute derived metrics from sums (keep original CTR from metrics).
        # NOTE: bounce_rate is NOT recomputed here — it is the real session-funnel
        # value carried from build_meta_ads_rollup (via meta-funnel by ad_id). The
        # old (clicks - _landing_page_view)/clicks formula produced a fake ~100.
        if '_initiate_checkout' in out.columns and '_landing_page_view' in out.columns:
            denom_vc = out['_landing_page_view'].replace(0, pd.NA)
        if 'shopify_revenue' in out.columns and 'spend' in out.columns:
            out['gross_roas'] = (out['shopify_revenue'] / out['spend']).replace([pd.NA, pd.NaT], 0).fillna(0)
        if {'shopify_revenue','shopify_cogs','spend'}.issubset(out.columns):
            # Compute Net ROAS at ad level for ad_rollup sheet
            with np.errstate(divide='ignore', invalid='ignore'):
                net = (out['shopify_revenue'] - out['shopify_cogs']) / out['spend']
            out['net_roas'] = pd.to_numeric(net, errors='coerce').replace([np.inf, -np.inf], 0).fillna(0)
            
            # Compute Net Profit - use SKU-level revenue/COGS if available, otherwise use ad-level
            # Distribute ad spend proportionally based on SKU revenue contribution
            if 'sku_revenue' in out.columns and 'sku_cogs' in out.columns:
                # Calculate SKU-level net profit using SKU revenue/COGS and proportional spend
                sku_rev = pd.to_numeric(out['sku_revenue'], errors='coerce').fillna(0)
                sku_cogs = pd.to_numeric(out['sku_cogs'], errors='coerce').fillna(0)
                ad_rev = pd.to_numeric(out['shopify_revenue'], errors='coerce').fillna(0)
                ad_spend = pd.to_numeric(out['spend'], errors='coerce').fillna(0)
                
                # Calculate proportional spend for each SKU based on revenue contribution
                # If ad revenue is 0, distribute spend equally among SKUs
                with np.errstate(divide='ignore', invalid='ignore'):
                    revenue_ratio = sku_rev / ad_rev.replace(0, pd.NA)
                    # For rows where ad_rev is 0 or ratio is invalid, distribute spend equally
                    # Count SKUs per ad group (using merge_keys which is in scope)
                    if len(merge_keys) > 0 and all(col in out.columns for col in merge_keys):
                        # Group by ad-level keys to count SKUs per ad
                        sku_counts = out.groupby(merge_keys, dropna=False).size()
                        sku_counts_dict = sku_counts.to_dict()
                        # Create a key for each row based on merge_keys
                        row_keys = out[merge_keys].apply(lambda x: tuple(x), axis=1)
                        equal_ratio = row_keys.map(lambda k: 1.0 / sku_counts_dict.get(k, 1))
                        # Use revenue ratio if valid, otherwise equal distribution
                        spend_ratio = revenue_ratio.fillna(equal_ratio)
                    else:
                        # If no merge_keys available, use revenue ratio or 1.0 as fallback
                        spend_ratio = revenue_ratio.fillna(1.0)
                
                # Calculate SKU-level net profit
                sku_spend = ad_spend * spend_ratio.fillna(0)
                out['net_profit'] = (sku_rev - sku_cogs - sku_spend).fillna(0)
            else:
                # Fallback to ad-level calculation if SKU columns not available
                out['net_profit'] = (out['shopify_revenue'] - out['shopify_cogs'] - out['spend']).fillna(0)
            
            out['profit_margin'] = (out['net_profit'] / out['shopify_revenue']).replace([pd.NA, pd.NaT], 0).fillna(0)
        if 'quantity' in out.columns:
            qty = out['quantity'].replace(0, pd.NA)
            if 'sku_revenue' in out.columns:
                out['unit_price'] = (out['sku_revenue'] / qty).fillna(0)
            if 'sku_cogs' in out.columns:
                out['unit_cost'] = (out['sku_cogs'] / qty).fillna(0)

    # Order columns: funnel order including SKU fields
    ordered_cols = order_columns_by_funnel(out, include_sku=True)
    final_result = out[ordered_cols]
    
    if not final_result.empty:
        print(f"[Meta Ads Rollup] Generated {len(final_result)} ad-SKU rows")
    
    return final_result


def _merge_repeating_values_in_sheet(
    writer: pd.ExcelWriter,
    df: pd.DataFrame,
    sheet_name: str,
    key_columns: list[str],
    scope_columns: list[str] | None = None,
    sum_columns: list[str] | None = None,
) -> None:
    """
    After writing df to the worksheet, merge repeating contiguous values for
    the given key_columns using xlsxwriter's merge_range, producing the
    grouped-look as in the screenshot.

    Assumes df has already been sorted by key_columns before writing.
    Excludes Grand Total rows from merging to keep them separate.
    """
    if df.empty:
        return

    worksheet = writer.sheets.get(sheet_name)
    if worksheet is None:
        return

    # Basic formats
    workbook = writer.book
    merge_fmt = workbook.add_format({'align': 'center', 'valign': 'vcenter'})

    # Work on a copy with positional index matching the sheet rows
    sorted_df = df.reset_index(drop=True)
    
    # Find Grand Total rows to exclude from merging
    grand_total_rows = set()
    for idx, row in sorted_df.iterrows():
        # Check if this is a Grand Total row
        is_grand_total = False
        if 'campaign_name' in row and str(row['campaign_name']).strip() == 'Grand Total':
            is_grand_total = True
        elif 'date_start' in row and str(row['date_start']).strip() == 'Total':
            is_grand_total = True
        elif 'channel' in row and str(row['channel']).strip() == 'All':
            is_grand_total = True
        
        if is_grand_total:
            grand_total_rows.add(idx)

    # Helper to sanitize value for Excel (avoid NaN/Inf errors)
    def _safe_value(val):
        if val is None or (isinstance(val, float) and (val != val)):
            return ''
        try:
            # handle pandas NA and numpy NaN/Inf
            if pd.isna(val):
                return ''
            if isinstance(val, (int, float, np.number)) and not np.isfinite(val):
                return ''
        except Exception:
            pass
        return val

    # For each level, find contiguous spans and merge, scoped by context columns if provided
    sum_columns = set(sum_columns or [])
    for col in key_columns:
        if col not in sorted_df.columns:
            continue
        col_idx = sorted_df.columns.get_loc(col)

        start = 0
        current = sorted_df.iloc[0][col]
        current_scope = tuple(sorted_df.iloc[0][scope_columns].tolist()) if scope_columns else None
        n = len(sorted_df)
        for i in range(1, n + 1):
            scope_differs = False
            if scope_columns and i < n:
                scope_differs = tuple(sorted_df.iloc[i][scope_columns].tolist()) != current_scope
            
            # Check if current row or next row is a Grand Total row
            is_current_grand_total = start in grand_total_rows
            is_next_grand_total = i < n and i in grand_total_rows
            
            # Don't merge across Grand Total boundaries
            is_break = (i == n or 
                       sorted_df.iloc[i][col] != current or 
                       scope_differs or 
                       is_current_grand_total or 
                       is_next_grand_total)
            
            if is_break:
                end = i - 1
                # Only merge if span > 1 and no Grand Total rows are involved
                if end > start and not is_current_grand_total:
                    # +1 for header row
                    if col in sum_columns:
                        try:
                            block_df = sorted_df.loc[start:end]
                            if col == 'shopify_revenue' and 'sku_revenue' in block_df.columns:
                                agg_val = pd.to_numeric(block_df['sku_revenue'], errors='coerce').fillna(0).sum()
                            elif col == 'shopify_cogs' and 'sku_cogs' in block_df.columns:
                                agg_val = pd.to_numeric(block_df['sku_cogs'], errors='coerce').fillna(0).sum()
                            else:
                                agg_val = pd.to_numeric(block_df[col], errors='coerce').fillna(0).sum()
                        except Exception:
                            agg_val = 0
                        worksheet.merge_range(start + 1, col_idx, end + 1, col_idx, round(float(agg_val), 2), merge_fmt)
                    else:
                        worksheet.merge_range(start + 1, col_idx, end + 1, col_idx, _safe_value(current), merge_fmt)
                start = i
                if i < n:
                    current = sorted_df.iloc[i][col]
                    if scope_columns:
                        current_scope = tuple(sorted_df.iloc[i][scope_columns].tolist())

def build_meta_ads_rollup(df: pd.DataFrame) -> pd.DataFrame:
    """
    Build Meta ads rollup similar to ad_rollup sheet from dailyrollup_orig.py.
    Groups by date, campaign, adset, ad and includes proper funnel metrics.
    """
    if df.empty:
        return pd.DataFrame()
    
    # Filter for Meta Ads only
    meta_df = df[df['source'] == 'Meta Ads'].copy()
    if meta_df.empty:
        return pd.DataFrame()
    
    # Transform the data
    meta_df = transform_attribution_data(meta_df)
    
    # Create unified funnel columns (exact same logic as dailyrollup_orig.py)
    off_view = 'action_offsite_pixel_view_content'
    off_atc = 'action_offsite_pixel_add_to_cart'
    off_ic = 'action_offsite_pixel_initiate_checkout'
    on_view = 'action_onsite_web_view_content'
    lp_view = 'action_landing_page_view'
    on_atc = 'action_onsite_web_add_to_cart'
    on_ic = 'action_onsite_web_initiate_checkout'
    
    # Landing page views (no offsite/onsite distinction here, take as-is if present)
    meta_df['_landing_page_view'] = meta_df.get(lp_view, pd.Series([0]*len(meta_df))).fillna(0)
    
    # Add to cart (prefer offsite when > 0, else onsite)
    meta_df['_add_to_cart'] = (
        meta_df.get(off_atc, pd.Series([pd.NA]*len(meta_df))).fillna(0)
            .where(meta_df.get(off_atc, pd.Series([0]*len(meta_df))).fillna(0) > 0,
                   meta_df.get(on_atc, pd.Series([0]*len(meta_df))).fillna(0))
    )
    
    # Initiate checkout (prefer offsite when > 0, else onsite)
    meta_df['_initiate_checkout'] = (
        meta_df.get(off_ic, pd.Series([pd.NA]*len(meta_df))).fillna(0)
            .where(meta_df.get(off_ic, pd.Series([0]*len(meta_df))).fillna(0) > 0,
                   meta_df.get(on_ic, pd.Series([0]*len(meta_df))).fillna(0))
    )
    
    # Group by campaign hierarchy
    group_cols = [
        'date_start','channel','campaign_name','adset_name','ad_name'
    ]
    present_group_cols = [c for c in group_cols if c in meta_df.columns]
    
    agg_dict = {
        'impressions':'sum',
        'clicks':'sum',
        'spend':'sum',
        'shopify_orders':'sum',
        'shopify_revenue':'sum',
        'shopify_cogs':'sum',
        'total_sku_quantity':'sum',
        '_landing_page_view':'sum',
        '_add_to_cart':'sum',
        '_initiate_checkout':'sum',
    }
    # Add ad_id as 'first' if available (for real funnel join by ad_id)
    if 'ad_id' in meta_df.columns:
        agg_dict['ad_id'] = 'first'

    present_agg = {k:v for k,v in agg_dict.items() if k in meta_df.columns}
    
    rollup = meta_df.groupby(present_group_cols, dropna=False).agg(present_agg).reset_index()
    
    # CTR: derive from summed clicks/impressions (percentage scale, matches campaign rollup)
    if {'clicks', 'impressions'}.issubset(rollup.columns):
        with np.errstate(divide='ignore', invalid='ignore'):
            ctr_calc = (
                pd.to_numeric(rollup['clicks'], errors='coerce')
                / pd.to_numeric(rollup['impressions'], errors='coerce')
            ) * 100
        rollup['ctr'] = pd.to_numeric(ctr_calc, errors='coerce').replace([np.inf, -np.inf], 0).fillna(0)
    else:
        rollup['ctr'] = 0
    
    if 'spend' in rollup.columns and 'shopify_revenue' in rollup.columns:
        rollup['gross_roas'] = (rollup['shopify_revenue'] / rollup['spend']).replace([pd.NA, pd.NaT], 0).fillna(0)
    if 'shopify_revenue' in rollup.columns and 'shopify_cogs' in rollup.columns and 'spend' in rollup.columns:
        rollup['net_profit'] = (rollup['shopify_revenue'] - rollup['shopify_cogs'] - rollup['spend']).fillna(0)
        rollup['profit_margin'] = (rollup['net_profit'] / rollup['shopify_revenue']).replace([pd.NA, pd.NaT], 0).fillna(0)
        with np.errstate(divide='ignore', invalid='ignore'):
            net = (rollup['shopify_revenue'] - rollup['shopify_cogs']) / rollup['spend']
        rollup['net_roas'] = pd.to_numeric(net, errors='coerce').replace([np.inf, -np.inf], 0).fillna(0)
    
    # Rates requested (exact same logic as dailyrollup_orig.py)
    if 'clicks' in rollup.columns and '_landing_page_view' in rollup.columns:
        # bounce_rate = (clicks - view_content) / clicks * 100
        denom = rollup['clicks'].replace(0, pd.NA)
        bounce = ((rollup['clicks'] - rollup['_landing_page_view']) / denom) * 100
        rollup['bounce_rate'] = bounce.fillna(0).infer_objects(copy=False).clip(lower=0, upper=100)
    if '_initiate_checkout' in rollup.columns and '_landing_page_view' in rollup.columns:
        denom_vc = rollup['_landing_page_view'].replace(0, pd.NA)
        cvr = (rollup['_initiate_checkout'] / denom_vc) * 100

    # --- Real on-site funnel metrics (override attribution-derived values) ---
    # Join /v1/meta-funnel by ad_id: real landing-page views, add-to-cart,
    # checkout starts and (crucially) the real session bounce rate — replacing
    # the fake 100 that came from (clicks - 0)/clicks because attribution has no
    # landing-page counts. Ads not present in the funnel keep their prior values.
    if 'ad_id' in rollup.columns and 'date_start' in meta_df.columns:
        try:
            _fs = str(meta_df['date_start'].min())[:10]
            _fe = str(meta_df['date_start'].max())[:10]
            by_ad, _ = _fetch_meta_funnel_by_ad(_fs, _fe)
            if by_ad:
                ids = rollup['ad_id'].astype(str).tolist()

                def _funnel_col(field, fallback_col):
                    fb = rollup[fallback_col].tolist() if fallback_col in rollup.columns else [0.0] * len(ids)
                    out = []
                    for i, a in enumerate(ids):
                        if a in by_ad:
                            out.append(by_ad[a][field])
                        else:
                            try:
                                out.append(float(fb[i] or 0))
                            except (TypeError, ValueError):
                                out.append(0.0)
                    return out

                rollup['_landing_page_view'] = _funnel_col('landing_page_views', '_landing_page_view')
                rollup['_add_to_cart'] = _funnel_col('add_to_cart', '_add_to_cart')
                rollup['_initiate_checkout'] = _funnel_col('checkout_start', '_initiate_checkout')
                rollup['bounce_rate'] = _funnel_col('bounce_rate', 'bounce_rate')
        except Exception as _funnel_exc:
            print(f"[Meta Funnel] enrichment skipped: {_funnel_exc}")

    # Order columns with funnel order (without SKU fields at this level)
    cols = order_columns_by_funnel(rollup, include_sku=False)
    # Sort rows for readability
    sort_cols = [c for c in ['date_start','channel','campaign_name','adset_name','ad_name'] if c in rollup.columns]
    if sort_cols:
        return rollup[cols].sort_values(sort_cols).reset_index(drop=True)
    return rollup[cols]

def build_meta_campaigns_rollup(df: pd.DataFrame) -> pd.DataFrame:
    """
    Build Meta campaigns rollup at campaign level by aggregating ad-level rollup.
    Deriving from ad rollup ensures spend/revenue/orders reconcile with meta_ads_rollup.
    """
    ad_rollup = build_meta_ads_rollup(df)
    if ad_rollup.empty:
        return pd.DataFrame()

    group_cols = [c for c in ['date_start', 'channel', 'campaign_name'] if c in ad_rollup.columns]
    if not group_cols:
        return pd.DataFrame()

    numeric_cols = [
        'impressions', 'clicks', 'spend', 'shopify_orders', 'shopify_revenue', 'shopify_cogs',
        '_landing_page_view', '_initiate_checkout', '_add_to_cart',
    ]
    for col in numeric_cols:
        if col in ad_rollup.columns:
            ad_rollup[col] = pd.to_numeric(ad_rollup[col], errors='coerce').fillna(0)

    agg_dict = {col: 'sum' for col in numeric_cols if col in ad_rollup.columns}
    summed = ad_rollup.groupby(group_cols, dropna=False).agg(agg_dict).reset_index()

    # Recalculate rate metrics from summed base columns (weighted, never averaged)
    if {'clicks', 'impressions'}.issubset(summed.columns):
        with np.errstate(divide='ignore', invalid='ignore'):
            ctr_calc = (
                pd.to_numeric(summed['clicks'], errors='coerce')
                / pd.to_numeric(summed['impressions'], errors='coerce')
            ) * 100
        summed['ctr'] = pd.to_numeric(ctr_calc, errors='coerce').replace([np.inf, -np.inf], 0).fillna(0)
    else:
        summed['ctr'] = 0
    if {'shopify_revenue', 'spend'}.issubset(summed.columns):
        with np.errstate(divide='ignore', invalid='ignore'):
            g = pd.to_numeric(summed['shopify_revenue'], errors='coerce') / pd.to_numeric(summed['spend'], errors='coerce')
        summed['gross_roas'] = pd.to_numeric(g, errors='coerce').replace([np.inf, -np.inf], 0).fillna(0)
    if {'shopify_revenue', 'shopify_cogs', 'spend'}.issubset(summed.columns):
        with np.errstate(divide='ignore', invalid='ignore'):
            n = (
                pd.to_numeric(summed['shopify_revenue'], errors='coerce')
                - pd.to_numeric(summed['shopify_cogs'], errors='coerce')
            ) / pd.to_numeric(summed['spend'], errors='coerce')
        summed['net_roas'] = pd.to_numeric(n, errors='coerce').replace([np.inf, -np.inf], 0).fillna(0)
        with np.errstate(divide='ignore', invalid='ignore'):
            b = (
                pd.to_numeric(summed['shopify_cogs'], errors='coerce')
                + pd.to_numeric(summed['spend'], errors='coerce')
            ) / pd.to_numeric(summed['spend'], errors='coerce')
        summed['be_roas'] = pd.to_numeric(b, errors='coerce').replace([np.inf, -np.inf], 0).fillna(0)
        summed['net_profit'] = (
            pd.to_numeric(summed['shopify_revenue'], errors='coerce')
            - pd.to_numeric(summed['shopify_cogs'], errors='coerce')
            - pd.to_numeric(summed['spend'], errors='coerce')
        ).fillna(0)
    if {'shopify_orders', 'clicks'}.issubset(summed.columns):
        with np.errstate(divide='ignore', invalid='ignore'):
            cr = (
                pd.to_numeric(summed['shopify_orders'], errors='coerce')
                / pd.to_numeric(summed['clicks'], errors='coerce')
            ) * 100
        summed['conversion_rate'] = pd.to_numeric(cr, errors='coerce').replace([np.inf, -np.inf], 0).fillna(0)

    desired = [
        'date_start', 'channel', 'campaign_name',
        'net_profit',
        'clicks',
        'ctr',
        'spend', 'shopify_revenue', 'shopify_cogs',
        'gross_roas', 'net_roas', 'be_roas', 'conversion_rate',
    ]
    present = [c for c in desired if c in summed.columns]
    out = summed[present].copy()
    out = round_for_output(out)
    if 'net_roas' in out.columns:
        secondary = [c for c in ['date_start', 'channel', 'campaign_name'] if c in out.columns]
        out = out.sort_values(['net_roas'] + secondary, ascending=[False] + [True] * len(secondary)).reset_index(drop=True)
    return out

def build_google_campaigns_rollup_with_sku(df: pd.DataFrame) -> pd.DataFrame:
    """
    Combine Google campaign-level performance metrics with SKU-level attribution so that
    each row represents a campaign and a single SKU attributed to it.
    """
    # Get campaign-level metrics
    metrics = build_google_campaigns_rollup(df)
    # Get SKU-level rollup for Google campaigns
    sku_rollup = build_ad_sku_rollup(df[df['source'] == 'Google Ads'])
    if not sku_rollup.empty:
        sku_group = ['date_start', 'channel', 'campaign_name', 'sku']
        sku_group = [c for c in sku_group if c in sku_rollup.columns]
        sku_rollup = sku_rollup.groupby(sku_group, dropna=False).agg({
            'sku_revenue': 'sum',
            'sku_cogs': 'sum',
            'quantity': 'sum',
            'unit_price': 'first',
            'unit_cost': 'first',
        }).reset_index()
    
    # Normalize key columns to ensure grouping/merging works even with stray spaces/case
    key_cols = ['date_start','channel','campaign_name','sku']
    for _df in [metrics, sku_rollup]:
        for c in key_cols:
            if c in _df.columns:
                _df[c] = _df[c].astype(str).str.strip()
    
    if metrics.empty:
        return pd.DataFrame()
    
    if sku_rollup.empty:
        # If no SKU data, return campaign metrics with empty SKU columns
        for col in ['sku', 'sku_revenue', 'sku_cogs', 'sku_quantity', 'unit_price', 'unit_cost']:
            if col not in metrics.columns:
                metrics[col] = ''
        return metrics
    
    # Merge campaign metrics with SKU data
    # For Google campaigns, we group by campaign_name (no adset/ad level)
    merged = metrics.merge(
        sku_rollup[['date_start','channel','campaign_name','sku','sku_revenue','sku_cogs','quantity','unit_price','unit_cost']],
        on=['date_start','channel','campaign_name'],
        how='left'
    )
    
    # Fill missing SKU data
    merged['sku'] = merged['sku'].fillna('')
    merged['sku_revenue'] = merged['sku_revenue'].fillna(0)
    merged['sku_cogs'] = merged['sku_cogs'].fillna(0)
    merged['quantity'] = merged['quantity'].fillna(0)
    merged['unit_price'] = merged['unit_price'].fillna(0)
    merged['unit_cost'] = merged['unit_cost'].fillna(0)
    
    # Rename quantity to sku_quantity for consistency
    merged = merged.rename(columns={'quantity': 'sku_quantity'})
    
    # Recalculate CTR using weighted average when merging
    if 'ctr' in merged.columns and 'impressions' in merged.columns:
        # Group by campaign and recalculate weighted CTR
        def recalc_ctr(group):
            if len(group) > 1:
                # Use the first row's CTR as base (from campaign rollup)
                base_ctr = group['ctr'].iloc[0]
                return base_ctr
            return group['ctr'].iloc[0]
        
        merged['ctr'] = merged.groupby(['date_start','channel','campaign_name'])['ctr'].transform(recalc_ctr)
    
    return merged

def build_organic_campaigns_rollup_with_sku(df: pd.DataFrame) -> pd.DataFrame:
    """
    Combine Organic campaign-level performance metrics with SKU-level attribution so that
    each row represents a campaign and a single SKU attributed to it.
    """
    # Get campaign-level metrics
    metrics = build_organic_campaigns_rollup(df)
    
    # Get SKU-level rollup for Organic campaigns
    organic_df = df[df['source'] == 'Organic'].copy()
    
    # Ensure campaign_name is set properly for organic data
    if 'campaign_name' not in organic_df.columns or organic_df['campaign_name'].isna().all():
        organic_df['campaign_name'] = 'Organic Traffic'
    else:
        organic_df['campaign_name'] = organic_df['campaign_name'].fillna('Organic Traffic')
    
    sku_rollup = build_ad_sku_rollup(organic_df)
    if not sku_rollup.empty:
        sku_group = ['date_start', 'channel', 'campaign_name', 'sku']
        sku_group = [c for c in sku_group if c in sku_rollup.columns]
        sku_rollup = sku_rollup.groupby(sku_group, dropna=False).agg({
            'sku_revenue': 'sum',
            'sku_cogs': 'sum',
            'quantity': 'sum',
            'unit_price': 'first',
            'unit_cost': 'first',
        }).reset_index()
    
    # Normalize key columns to ensure grouping/merging works even with stray spaces/case
    key_cols = ['date_start','channel','campaign_name','sku']
    for _df in [metrics, sku_rollup]:
        for c in key_cols:
            if c in _df.columns:
                _df[c] = _df[c].astype(str).str.strip()
    
    if metrics.empty:
        return pd.DataFrame()
    
    if sku_rollup.empty:
        # If no SKU data, return campaign metrics with empty SKU columns
        for col in ['sku', 'sku_revenue', 'sku_cogs', 'sku_quantity', 'unit_price', 'unit_cost']:
            if col not in metrics.columns:
                metrics[col] = ''
        return metrics
    
    # Merge campaign metrics with SKU data
    # For Organic campaigns, we group by campaign_name (no adset/ad level)
    merged = metrics.merge(
        sku_rollup[['date_start','channel','campaign_name','sku','sku_revenue','sku_cogs','quantity','unit_price','unit_cost']],
        on=['date_start','channel','campaign_name'],
        how='left'
    )
    
    # Fill missing SKU data
    merged['sku'] = merged['sku'].fillna('')
    merged['sku_revenue'] = merged['sku_revenue'].fillna(0)
    merged['sku_cogs'] = merged['sku_cogs'].fillna(0)
    merged['quantity'] = merged['quantity'].fillna(0)
    merged['unit_price'] = merged['unit_price'].fillna(0)
    merged['unit_cost'] = merged['unit_cost'].fillna(0)
    
    # Rename quantity to sku_quantity for consistency
    merged = merged.rename(columns={'quantity': 'sku_quantity'})
    
    # Recalculate CTR using weighted average when merging
    if 'ctr' in merged.columns and 'impressions' in merged.columns:
        # Group by campaign and recalculate weighted CTR
        def recalc_ctr(group):
            if len(group) > 1:
                # Use the first row's CTR as base (from campaign rollup)
                base_ctr = group['ctr'].iloc[0]
                return base_ctr
            return group['ctr'].iloc[0]
        
        merged['ctr'] = merged.groupby(['date_start','channel','campaign_name'])['ctr'].transform(recalc_ctr)
    
    return merged

def build_google_campaigns_rollup(df: pd.DataFrame) -> pd.DataFrame:
    """
    Build Google campaigns rollup at campaign level.
    """
    if df.empty:
        return pd.DataFrame()
    
    # Filter for Google Ads only
    google_df = df[df['source'] == 'Google Ads'].copy()
    if google_df.empty:
        return pd.DataFrame()
    
    # Transform the data
    google_df = transform_attribution_data(google_df)
    
    # Group by campaign level
    group_cols = [c for c in ['date_start','channel','campaign_name'] if c in google_df.columns]
    if not group_cols:
        return pd.DataFrame()
    
    # Ensure numeric columns
    numeric_cols = [
        'impressions','clicks','spend','shopify_orders','shopify_revenue','shopify_cogs'
    ]
    for col in numeric_cols:
        if col in google_df.columns:
            google_df[col] = pd.to_numeric(google_df[col], errors='coerce').fillna(0)
    
    summed = google_df.groupby(group_cols, dropna=False)[numeric_cols].sum().reset_index()
    
    # Recalculate funnel metrics from sums (same logic as Meta campaigns)
    # CTR: weighted clicks/impressions — API ctr is already a percentage, never average it
    if {'clicks','impressions'}.issubset(summed.columns):
        with np.errstate(divide='ignore', invalid='ignore'):
            ctr_calc = (pd.to_numeric(summed['clicks'], errors='coerce') / pd.to_numeric(summed['impressions'], errors='coerce')) * 100
        summed['ctr'] = pd.to_numeric(ctr_calc, errors='coerce').replace([np.inf, -np.inf], 0).fillna(0)
    else:
        summed['ctr'] = 0
    
    # Calculate bounce rate (same logic as Meta campaigns)
    if {'clicks','_landing_page_view'}.issubset(summed.columns):
        with np.errstate(divide='ignore', invalid='ignore'):
            bounce = ((pd.to_numeric(summed['clicks'], errors='coerce') - pd.to_numeric(summed['_landing_page_view'], errors='coerce')) / 
                     pd.to_numeric(summed['clicks'], errors='coerce') * 100)
        summed['bounce_rate'] = pd.to_numeric(bounce, errors='coerce').replace([np.inf, -np.inf], 0).fillna(0).clip(lower=0, upper=100)
    if {'shopify_revenue','spend'}.issubset(summed.columns):
        with np.errstate(divide='ignore', invalid='ignore'):
            g = pd.to_numeric(summed['shopify_revenue'], errors='coerce') / pd.to_numeric(summed['spend'], errors='coerce')
        summed['gross_roas'] = pd.to_numeric(g, errors='coerce').replace([np.inf, -np.inf], 0).fillna(0)
    if {'shopify_revenue','shopify_cogs','spend'}.issubset(summed.columns):
        with np.errstate(divide='ignore', invalid='ignore'):
            n = (pd.to_numeric(summed['shopify_revenue'], errors='coerce') - pd.to_numeric(summed['shopify_cogs'], errors='coerce')) / pd.to_numeric(summed['spend'], errors='coerce')
        summed['net_roas'] = pd.to_numeric(n, errors='coerce').replace([np.inf, -np.inf], 0).fillna(0)
        with np.errstate(divide='ignore', invalid='ignore'):
            b = (pd.to_numeric(summed['shopify_cogs'], errors='coerce') + pd.to_numeric(summed['spend'], errors='coerce')) / pd.to_numeric(summed['spend'], errors='coerce')
        summed['be_roas'] = pd.to_numeric(b, errors='coerce').replace([np.inf, -np.inf], 0).fillna(0)
        # Calculate net profit: revenue - cogs - spend
        summed['net_profit'] = (pd.to_numeric(summed['shopify_revenue'], errors='coerce') - 
                               pd.to_numeric(summed['shopify_cogs'], errors='coerce') - 
                               pd.to_numeric(summed['spend'], errors='coerce')).fillna(0)
    # Conversion rate: purchases / clicks * 100
    if {'shopify_orders','clicks'}.issubset(summed.columns):
        with np.errstate(divide='ignore', invalid='ignore'):
            cr = (pd.to_numeric(summed['shopify_orders'], errors='coerce') / pd.to_numeric(summed['clicks'], errors='coerce')) * 100
        summed['conversion_rate'] = pd.to_numeric(cr, errors='coerce').replace([np.inf, -np.inf], 0).fillna(0)
    
    # Select and order requested columns when present
    desired = [
        'date_start','channel','campaign_name',
        'net_profit',
        'ctr',
        'spend','shopify_revenue','shopify_cogs','shopify_orders','bounce_rate',
        'gross_roas','net_roas','conversion_rate'
    ]
    present = [c for c in desired if c in summed.columns]
    out = summed[present].copy()
    out = round_for_output(out)
    # Sort by net_roas descending, then by keys for stability
    if 'net_roas' in out.columns:
        secondary = [c for c in ['date_start','channel','campaign_name'] if c in out.columns]
        out = out.sort_values(['net_roas'] + secondary, ascending=[False] + [True]*len(secondary)).reset_index(drop=True)
    return out

def build_organic_campaigns_rollup(df: pd.DataFrame) -> pd.DataFrame:
    """
    Build Organic campaigns rollup at campaign level.
    """
    if df.empty:
        return pd.DataFrame()
    
    # Filter for Organic only
    organic_df = df[df['source'] == 'Organic'].copy()
    if organic_df.empty:
        return pd.DataFrame()
    
    # Transform the data
    organic_df = transform_attribution_data(organic_df)
    
    # For organic data, ensure campaign_name is set properly
    # If campaign_name is missing or None, use a default value
    if 'campaign_name' not in organic_df.columns or organic_df['campaign_name'].isna().all():
        organic_df['campaign_name'] = 'Organic Traffic'
    else:
        # Fill any None/NaN values with a default
        organic_df['campaign_name'] = organic_df['campaign_name'].fillna('Organic Traffic')
    
    # Group by campaign level
    group_cols = [c for c in ['date_start','channel','campaign_name'] if c in organic_df.columns]
    if not group_cols:
        return pd.DataFrame()
    
    # Ensure numeric columns
    numeric_cols = [
        'impressions','clicks','spend','shopify_orders','shopify_revenue','shopify_cogs'
    ]
    for col in numeric_cols:
        if col in organic_df.columns:
            organic_df[col] = pd.to_numeric(organic_df[col], errors='coerce').fillna(0)
    
    summed = organic_df.groupby(group_cols, dropna=False)[numeric_cols].sum().reset_index()
    
    # For organic campaigns, exclude ctr, spend, net_roas, be_roas, conversion_rate, net_profit, gross_roas
    
    # Select and order requested columns when present (exclude ctr, spend, net_roas, be_roas, conversion_rate, net_profit, gross_roas)
    desired = [
        'date_start','channel','campaign_name',
        'shopify_revenue','shopify_cogs'
    ]
    present = [c for c in desired if c in summed.columns]
    out = summed[present].copy()
    out = round_for_output(out)
    # Sort by shopify_revenue descending, then by keys for stability
    if 'shopify_revenue' in out.columns:
        secondary = [c for c in ['date_start','channel','campaign_name'] if c in out.columns]
        out = out.sort_values(['shopify_revenue'] + secondary, ascending=[False] + [True]*len(secondary)).reset_index(drop=True)
    return out

def get_campaign_data(start_date: str | None = None, end_date: str | None = None) -> pd.DataFrame:
    """
    Return campaign-level data for Meta channel only, aggregated at campaign level (no SKU granularity).
    Returns data in the format expected by the PDF report.

    Returns columns (when available):
      - From campaign_rollup sheet: date_start, channel, campaign_name, spend, shopify_revenue,
        ctr, bounce_rate, gross_roas, net_roas, be_roas, conversion_rate
      - Extras for PDF: cogs, net_profit, purchases, clicks, roas, breakeven_roas, cpp
    """
    try:
        # Resolve timeframe if not provided
        tf = get_timeframe_config(start_date, end_date)
        s = tf['start_date'].strftime('%Y-%m-%d')
        e = tf['end_date'].strftime('%Y-%m-%d')

        # Fetch raw data
        raw = fetch_marketing_hourly(s, e)
        if raw.empty:
            return pd.DataFrame()

        # Filter for Meta Ads source only (consistent with build_meta_campaigns_rollup)
        meta_raw = raw[raw['source'] == 'Meta Ads'].copy()
        if meta_raw.empty:
            return pd.DataFrame()

        # Build campaign-level rollup for Meta only
        camp = build_meta_campaigns_rollup(meta_raw)
        if camp.empty:
            return pd.DataFrame()

        # Map to PDF-friendly columns
        out = camp.copy()
        
        # Derive additional columns for PDF
        if {'shopify_revenue','shopify_cogs','spend'}.issubset(out.columns):
            out['net_profit'] = (pd.to_numeric(out['shopify_revenue'], errors='coerce').fillna(0)
                                 - pd.to_numeric(out['shopify_cogs'], errors='coerce').fillna(0)
                                 - pd.to_numeric(out['spend'], errors='coerce').fillna(0))
        
        if {'shopify_revenue','spend'}.issubset(out.columns) and 'gross_roas' not in out.columns:
            with np.errstate(divide='ignore', invalid='ignore'):
                out['gross_roas'] = pd.to_numeric(out['shopify_revenue'], errors='coerce') / pd.to_numeric(out['spend'], errors='coerce')
            out['gross_roas'] = out['gross_roas'].replace([np.inf, -np.inf], 0).fillna(0)
        
        if {'shopify_revenue','shopify_cogs','spend'}.issubset(out.columns) and 'net_roas' not in out.columns:
            with np.errstate(divide='ignore', invalid='ignore'):
                out['net_roas'] = (pd.to_numeric(out['shopify_revenue'], errors='coerce') - pd.to_numeric(out['shopify_cogs'], errors='coerce')) / pd.to_numeric(out['spend'], errors='coerce')
            out['net_roas'] = out['net_roas'].replace([np.inf, -np.inf], 0).fillna(0)
        
        if {'shopify_cogs','spend'}.issubset(out.columns) and 'be_roas' not in out.columns:
            with np.errstate(divide='ignore', invalid='ignore'):
                out['be_roas'] = (pd.to_numeric(out['shopify_cogs'], errors='coerce') + pd.to_numeric(out['spend'], errors='coerce')) / pd.to_numeric(out['spend'], errors='coerce')
            out['be_roas'] = out['be_roas'].replace([np.inf, -np.inf], 0).fillna(0)

        # Rename for PDF extras - ensure 'sales' column exists for excel_generation.py compatibility
        rename_map = {
            'shopify_cogs': 'cogs',
            'shopify_orders': 'purchases',
            'shopify_revenue': 'sales',  # Add this mapping for excel_generation.py compatibility
        }
        out = out.rename(columns=rename_map)

        # Clicks needed for PDF; recompute from raw if missing
        if 'clicks' not in out.columns and 'clicks' in meta_raw.columns:
            # Aggregate clicks at campaign level from raw Meta data
            clicks_sum = meta_raw.groupby(['date_start','channel','campaign_name'], dropna=False)['clicks'].sum().reset_index()
            out = out.merge(clicks_sum, on=['date_start','channel','campaign_name'], how='left')

        # ROAS alias for PDF
        if 'roas' not in out.columns and 'gross_roas' in out.columns:
            out['roas'] = out['gross_roas']
        if 'breakeven_roas' not in out.columns and 'be_roas' in out.columns:
            out['breakeven_roas'] = out['be_roas']
        if 'cpp' not in out.columns and {'spend','purchases'}.issubset(out.columns):
            with np.errstate(divide='ignore', invalid='ignore'):
                out['cpp'] = pd.to_numeric(out['spend'], errors='coerce') / pd.to_numeric(out['purchases'], errors='coerce')
            out['cpp'] = out['cpp'].replace([np.inf, -np.inf], 0).fillna(0)

        # Ensure both 'sales' and 'shopify_revenue' columns exist for compatibility
        if 'sales' not in out.columns and 'shopify_revenue' in out.columns:
            out['sales'] = out['shopify_revenue']
        elif 'shopify_revenue' not in out.columns and 'sales' in out.columns:
            out['shopify_revenue'] = out['sales']
        
        # Final column set and rounding
        desired_cols = [
            # Campaign rollup sheet columns
            'date_start','channel','campaign_name',
            'net_profit',
            'ctr',
            'spend','sales','shopify_revenue','bounce_rate',
            'gross_roas','net_roas','be_roas','conversion_rate',
            'impressions',
            # PDF extras
            'cogs','purchases','clicks','roas','breakeven_roas','cpp'
        ]
        present = [c for c in desired_cols if c in out.columns]
        out = out[present].copy()
        out = round_for_output(out)
        
        print(f"[get_campaign_data] Returning {len(out)} Meta campaigns")
        
        return out
    except Exception as e:
        print(f"[get_campaign_data] Error: {e}")
        return pd.DataFrame()


def get_meta_funnel_metrics(start_date: str | None = None, end_date: str | None = None) -> dict:
    """
    Get Meta channel funnel metrics with drop-off percentages for the performance metrics section.
    
    Returns funnel data for: Impressions → Clicks → Landing Page Views → Add to Cart → Checkout → Orders → Profit
    
    Returns:
        dict: Funnel metrics with counts and drop-off percentages for Meta channel
    """
    try:
        tf = get_timeframe_config(start_date, end_date)
        s = tf['start_date'].strftime('%Y-%m-%d')
        e = tf['end_date'].strftime('%Y-%m-%d')

        df = fetch_marketing_hourly(s, e)
        if df is None or df.empty:
            return {
                'impressions': 0, 'clicks': 0, 'landing_page_views': 0, 'add_to_cart': 0,
                'checkout': 0, 'orders': 0, 'net_profit': 0.0,
                'ctr': 0.0, 'landing_page_rate': 0.0, 'add_to_cart_rate': 0.0,
                'checkout_rate': 0.0, 'conversion_rate': 0.0, 'profit_per_order': 0.0,
                'drop_off_impressions_to_clicks': 0.0, 'drop_off_clicks_to_landing': 0.0,
                'drop_off_landing_to_cart': 0.0, 'drop_off_cart_to_checkout': 0.0,
                'drop_off_checkout_to_orders': 0.0
            }

        # Filter for Meta Ads source only (consistent with other functions)
        meta_df = df[df['source'] == 'Meta Ads'].copy()
        if meta_df.empty:
            return {
                'impressions': 0, 'clicks': 0, 'landing_page_views': 0, 'add_to_cart': 0,
                'checkout': 0, 'orders': 0, 'net_profit': 0.0,
                'ctr': 0.0, 'landing_page_rate': 0.0, 'add_to_cart_rate': 0.0,
                'checkout_rate': 0.0, 'conversion_rate': 0.0, 'profit_per_order': 0.0,
                'drop_off_impressions_to_clicks': 0.0, 'drop_off_clicks_to_landing': 0.0,
                'drop_off_landing_to_cart': 0.0, 'drop_off_cart_to_checkout': 0.0,
                'drop_off_checkout_to_orders': 0.0
            }

        # Transform the data to map column names (same as other functions)
        meta_df = transform_attribution_data(meta_df)

        # Ensure numeric columns
        numeric_cols = [
            'impressions', 'clicks', 'spend', 'shopify_orders', 'shopify_revenue', 'shopify_cogs'
        ]
        for col in numeric_cols:
            if col in meta_df.columns:
                meta_df[col] = pd.to_numeric(meta_df[col], errors='coerce').fillna(0)

        # Create unified funnel columns (same logic as build_campaign_rollup)
        off_atc = 'action_offsite_pixel_add_to_cart'
        on_atc = 'action_onsite_web_add_to_cart'
        off_ic = 'action_offsite_pixel_initiate_checkout'
        on_ic = 'action_onsite_web_initiate_checkout'
        lp_view = 'action_landing_page_view'

        # Landing page views
        meta_df['_landing_page_view'] = meta_df.get(lp_view, pd.Series([0]*len(meta_df))).fillna(0)
        
        # Add to cart (prefer offsite when > 0, else onsite)
        meta_df['_add_to_cart'] = (
            meta_df.get(off_atc, pd.Series([pd.NA]*len(meta_df))).fillna(0)
                .where(meta_df.get(off_atc, pd.Series([0]*len(meta_df))).fillna(0) > 0,
                       meta_df.get(on_atc, pd.Series([0]*len(meta_df))).fillna(0))
        )
        
        # Initiate checkout (prefer offsite when > 0, else onsite)
        meta_df['_initiate_checkout'] = (
            meta_df.get(off_ic, pd.Series([pd.NA]*len(meta_df))).fillna(0)
                .where(meta_df.get(off_ic, pd.Series([0]*len(meta_df))).fillna(0) > 0,
                       meta_df.get(on_ic, pd.Series([0]*len(meta_df))).fillna(0))
        )

        # Ensure funnel columns are numeric
        for col in ['_landing_page_view', '_add_to_cart', '_initiate_checkout']:
            if col in meta_df.columns:
                meta_df[col] = pd.to_numeric(meta_df[col], errors='coerce').fillna(0)

        attributed_orders_total = _meta_attributed_orders_total(s, e)

        # Calculate totals for Meta channel
        impressions = float(meta_df['impressions'].sum()) if 'impressions' in meta_df.columns else 0.0
        clicks = float(meta_df['clicks'].sum()) if 'clicks' in meta_df.columns else 0.0
        landing_page_views = float(meta_df['_landing_page_view'].sum()) if '_landing_page_view' in meta_df.columns else 0.0
        add_to_cart = float(meta_df['_add_to_cart'].sum()) if '_add_to_cart' in meta_df.columns else 0.0
        checkout = float(meta_df['_initiate_checkout'].sum()) if '_initiate_checkout' in meta_df.columns else 0.0
        orders = float(attributed_orders_total)
        revenue = float(meta_df['shopify_revenue'].sum()) if 'shopify_revenue' in meta_df.columns else 0.0
        cogs = float(meta_df['shopify_cogs'].sum()) if 'shopify_cogs' in meta_df.columns else 0.0
        spend = float(meta_df['spend'].sum()) if 'spend' in meta_df.columns else 0.0
        net_profit = revenue - cogs - spend

        # Calculate rates
        ctr = (clicks / impressions * 100.0) if impressions > 0 else 0.0
        landing_page_rate = (landing_page_views / clicks * 100.0) if clicks > 0 else 0.0
        add_to_cart_rate = (add_to_cart / landing_page_views * 100.0) if landing_page_views > 0 else 0.0
        checkout_rate = (checkout / add_to_cart * 100.0) if add_to_cart > 0 else 0.0
        conversion_rate = (orders / clicks * 100.0) if clicks > 0 else 0.0
        profit_per_order = (net_profit / orders) if orders > 0 else 0.0

        # Calculate drop-off percentages
        drop_off_impressions_to_clicks = ((impressions - clicks) / impressions * 100.0) if impressions > 0 else 0.0
        drop_off_clicks_to_landing = ((clicks - landing_page_views) / clicks * 100.0) if clicks > 0 else 0.0
        drop_off_landing_to_cart = ((landing_page_views - add_to_cart) / landing_page_views * 100.0) if landing_page_views > 0 else 0.0
        drop_off_cart_to_checkout = ((add_to_cart - checkout) / add_to_cart * 100.0) if add_to_cart > 0 else 0.0
        drop_off_checkout_to_orders = ((checkout - orders) / checkout * 100.0) if checkout > 0 else 0.0
        drop_off_cart_to_orders = ((add_to_cart - orders) / add_to_cart * 100.0) if add_to_cart > 0 else 0.0

        return {
            'impressions': int(round(impressions)),
            'clicks': int(round(clicks)),
            'landing_page_views': int(round(landing_page_views)),
            'add_to_cart': int(round(add_to_cart)),
            'checkout': int(round(checkout)),
            'orders': int(round(orders)),
            'net_profit': float(round(net_profit, 2)),
            'ctr': float(round(ctr, 2)),
            'landing_page_rate': float(round(landing_page_rate, 2)),
            'add_to_cart_rate': float(round(add_to_cart_rate, 2)),
            'checkout_rate': float(round(checkout_rate, 2)),
            'conversion_rate': float(round(conversion_rate, 2)),
            'profit_per_order': float(round(profit_per_order, 2)),
            'drop_off_impressions_to_clicks': float(round(drop_off_impressions_to_clicks, 2)),
            'drop_off_clicks_to_landing': float(round(drop_off_clicks_to_landing, 2)),
            'drop_off_landing_to_cart': float(round(drop_off_landing_to_cart, 2)),
            'drop_off_cart_to_checkout': float(round(drop_off_cart_to_checkout, 2)),
            'drop_off_checkout_to_orders': float(round(drop_off_checkout_to_orders, 2)),
            'drop_off_cart_to_orders': float(round(drop_off_cart_to_orders, 2))
        }
    except Exception as e:
        print(f"Error getting Meta funnel metrics: {e}")
        return {
            'impressions': 0, 'clicks': 0, 'landing_page_views': 0, 'add_to_cart': 0,
            'checkout': 0, 'orders': 0, 'net_profit': 0.0,
            'ctr': 0.0, 'landing_page_rate': 0.0, 'add_to_cart_rate': 0.0,
            'checkout_rate': 0.0, 'conversion_rate': 0.0, 'profit_per_order': 0.0,
            'drop_off_impressions_to_clicks': 0.0, 'drop_off_clicks_to_landing': 0.0,
            'drop_off_landing_to_cart': 0.0, 'drop_off_cart_to_checkout': 0.0,
            'drop_off_checkout_to_orders': 0.0, 'drop_off_cart_to_orders': 0.0
        }


def get_campaign_grand_total_for_pdf(start_date: str | None = None, end_date: str | None = None) -> dict:
    """
    Compute Meta channel grand total for the timeframe and return exactly the fields
    expected by the PDF Overall row in excel_generation.py.
    
    This function focuses on Meta channel data only to provide proper Meta totals
    for the PDF report's overall summary.

    Returned keys:
      - spend, shopify_revenue, ctr, bounce_rate,
        gross_roas, net_roas, be_roas, conversion_rate,
        impressions, clicks, shopify_orders, lpv, shopify_cogs, net_profit
    """
    try:
        tf = get_timeframe_config(start_date, end_date)
        s = tf['start_date'].strftime('%Y-%m-%d')
        e = tf['end_date'].strftime('%Y-%m-%d')

        df = fetch_marketing_hourly(s, e)
        if df is None or df.empty:
            return {
                'spend': 0.0, 'shopify_revenue': 0.0, 'shopify_cogs': 0.0,
                'impressions': 0, 'clicks': 0, 'shopify_orders': 0, 'lpv': 0.0,
                'ctr': 0.0, 'bounce_rate': 0.0,
                'gross_roas': 0.0, 'net_roas': 0.0, 'be_roas': 0.0, 'conversion_rate': 0.0,
                'net_profit': 0.0,
            }

        # Filter for Meta Ads source only (consistent with other functions)
        meta_df = df[df['source'] == 'Meta Ads'].copy()
        if meta_df.empty:
            return {
                'spend': 0.0, 'shopify_revenue': 0.0, 'shopify_cogs': 0.0,
                'impressions': 0, 'clicks': 0, 'shopify_orders': 0, 'lpv': 0.0,
                'ctr': 0.0, 'bounce_rate': 0.0,
                'gross_roas': 0.0, 'net_roas': 0.0, 'be_roas': 0.0, 'conversion_rate': 0.0,
                'net_profit': 0.0,
            }

        # Transform the data to map column names (same as other functions)
        meta_df = transform_attribution_data(meta_df)

        # Ensure numeric for base metrics
        for c in ['impressions','clicks','spend','shopify_orders','shopify_revenue','shopify_cogs']:
            if c in meta_df.columns:
                meta_df[c] = pd.to_numeric(meta_df[c], errors='coerce').fillna(0)

        # Derive unified funnel columns exactly like build_campaign_rollup
        off_ic = 'action_offsite_pixel_initiate_checkout'
        on_ic = 'action_onsite_web_initiate_checkout'
        lp_view = 'action_landing_page_view'

        try:
            if lp_view in meta_df.columns:
                meta_df['_landing_page_view'] = pd.to_numeric(meta_df[lp_view], errors='coerce').fillna(0)
            else:
                meta_df['_landing_page_view'] = 0
        except Exception:
            meta_df['_landing_page_view'] = 0

        try:
            off_ic_series = pd.to_numeric(meta_df.get(off_ic, 0), errors='coerce').fillna(0) if off_ic in meta_df.columns else 0
            on_ic_series = pd.to_numeric(meta_df.get(on_ic, 0), errors='coerce').fillna(0) if on_ic in meta_df.columns else 0
            if isinstance(off_ic_series, (int, float)):
                off_ic_series = pd.Series([off_ic_series] * len(meta_df))
            if isinstance(on_ic_series, (int, float)):
                on_ic_series = pd.Series([on_ic_series] * len(meta_df))
            meta_df['_initiate_checkout'] = off_ic_series.where(off_ic_series > 0, on_ic_series)
        except Exception:
            meta_df['_initiate_checkout'] = 0

        # Build Meta campaign-level rollup to get proper Meta totals
        ad_rollup = build_meta_ads_rollup(meta_df)
        meta_campaign_level = build_meta_campaigns_rollup(meta_df)

        if not ad_rollup.empty:
            spend = float(ad_rollup['spend'].sum()) if 'spend' in ad_rollup.columns else 0.0
            impressions = float(ad_rollup['impressions'].sum()) if 'impressions' in ad_rollup.columns else 0.0
            clicks = float(ad_rollup['clicks'].sum()) if 'clicks' in ad_rollup.columns else 0.0
            orders = float(ad_rollup['shopify_orders'].sum()) if 'shopify_orders' in ad_rollup.columns else 0.0
            revenue = float(ad_rollup['shopify_revenue'].sum()) if 'shopify_revenue' in ad_rollup.columns else 0.0
            cogs = float(ad_rollup['shopify_cogs'].sum()) if 'shopify_cogs' in ad_rollup.columns else 0.0
        elif not meta_campaign_level.empty:
            # Fallback to Meta raw data if campaign_level is empty
            impressions = float(meta_df['impressions'].sum()) if 'impressions' in meta_df.columns else 0.0
            clicks = float(meta_df['clicks'].sum()) if 'clicks' in meta_df.columns else 0.0
            spend = float(meta_df['spend'].sum()) if 'spend' in meta_df.columns else 0.0
            orders = float(meta_df['shopify_orders'].sum()) if 'shopify_orders' in meta_df.columns else 0.0
            revenue = float(meta_df['shopify_revenue'].sum()) if 'shopify_revenue' in meta_df.columns else 0.0
            cogs = float(meta_df['shopify_cogs'].sum()) if 'shopify_cogs' in meta_df.columns else 0.0
        
        # Calculate funnel metrics from Meta raw data
        lpv = float(meta_df['_landing_page_view'].sum()) if '_landing_page_view' in meta_df.columns else 0.0
        ic = float(meta_df['_initiate_checkout'].sum()) if '_initiate_checkout' in meta_df.columns else 0.0

        ctr = (clicks / impressions * 100.0) if impressions > 0 else 0.0
        # Guard against impossible lpv > clicks scenario: clamp lpv to clicks
        if lpv > clicks and clicks > 0:
            lpv = clicks
        bounce_rate = ((clicks - lpv) / clicks * 100.0) if clicks > 0 else 0.0
        checkout_cr = (ic / lpv * 100.0) if lpv > 0 else 0.0
        gross_roas = (revenue / spend) if spend > 0 else 0.0
        net_roas = ((revenue - cogs) / spend) if spend > 0 else 0.0
        be_roas = ((cogs + spend) / spend) if spend > 0 else 0.0
        # Net profit calculation: revenue - cogs - spend
        net_profit = revenue - cogs - spend
        # Conversion Rate consistent with ad rollup reporting: purchases / clicks * 100
        conv_rate = (orders / clicks * 100.0) if clicks > 0 else 0.0

        result = {
            'spend': float(round(spend, 2)),
            'shopify_revenue': float(round(revenue, 2)),
            'shopify_cogs': float(round(cogs, 2)),
            'impressions': int(round(impressions)),
            'clicks': int(round(clicks)),
            'shopify_orders': int(round(orders)),
            'lpv': float(round(lpv, 2)),
            'initiate_checkout': float(round(ic, 2)),
            'ctr': float(round(ctr, 2)),
            'bounce_rate': float(round(bounce_rate, 2)),
            'gross_roas': float(round(gross_roas, 2)),
            'net_roas': float(round(net_roas, 2)),
            'be_roas': float(round(be_roas, 2)),
            'conversion_rate': float(round(conv_rate, 2)),
            'net_profit': float(round(net_profit, 2)),
        }

        print(
            f"[get_campaign_grand_total_for_pdf] "
            f"spend={result['spend']:.2f}, revenue={result['shopify_revenue']:.2f}, "
            f"net_profit={result['net_profit']:.2f}, campaigns={len(meta_df)}"
        )

        return result
    except Exception as e:
        print(f"[get_campaign_grand_total_for_pdf] Error: {e}")
        return {
            'spend': 0.0, 'shopify_revenue': 0.0, 'shopify_cogs': 0.0,
            'impressions': 0, 'clicks': 0, 'shopify_orders': 0, 'lpv': 0.0,
                'ctr': 0.0, 'bounce_rate': 0.0,
            'gross_roas': 0.0, 'net_roas': 0.0, 'be_roas': 0.0, 'conversion_rate': 0.0,
            'net_profit': 0.0,
        }


def _write_amazon_sp_sheet(writer, start_date_str: str, end_date_str: str, sheet_name: str = "amazon_sp", days_lag: int = 1) -> None:
    """Write an Amazon SP (Seller Partner) line-item P&L sheet.

    One row per (amazon_order_id, seller_sku) with order-level P&L allocated down
    to each line. Reuses the proven builders in amazon_entity_report.py. Amazon gold
    tables lag ~1 day, so the window end is shifted back by days_lag; if that makes a
    single-day (today) report invalid, the last complete day is used instead.
    """
    try:
        from datetime import datetime as _dt, timedelta as _td
        from amazon_entity_report import (
            fetch_amazon_sp_items_gold,
            fetch_amazon_sp_orders_gold,
            fetch_amazon_sp_order_pnl_gold,
            _build_sp_line_items_display,
            _apply_sp_sheet_formatting,
        )

        s = _dt.strptime(str(start_date_str)[:10], "%Y-%m-%d")
        e = _dt.strptime(str(end_date_str)[:10], "%Y-%m-%d") - _td(days=days_lag)
        if e < s:
            s = e  # single last-complete day (respect the ~1-day gold lag)
        ss, ee = s.strftime("%Y-%m-%d"), e.strftime("%Y-%m-%d")

        items = fetch_amazon_sp_items_gold(ss, ee)
        orders = fetch_amazon_sp_orders_gold(ss, ee)
        pnl = fetch_amazon_sp_order_pnl_gold(ss, ee)
        sp_display = _build_sp_line_items_display(items, orders, pnl)

        if sp_display is not None and not sp_display.empty:
            sp_display = round_for_output(sp_display)
            sp_display.to_excel(writer, sheet_name=sheet_name, index=False)
            try:
                _apply_sp_sheet_formatting(writer, sheet_name, sp_display)
            except Exception as fe:
                print(f"[Amazon SP] formatting error: {fe}")
            print(f"[Amazon SP] Wrote {len(sp_display)} line items for {ss} to {ee}")
        else:
            pd.DataFrame().to_excel(writer, sheet_name=sheet_name, index=False)
            print(f"[Amazon SP] No data for {ss} to {ee}; wrote empty sheet")
    except Exception as ex:
        print(f"[Amazon SP] Error building sheet: {ex}")
        try:
            pd.DataFrame().to_excel(writer, sheet_name=sheet_name, index=False)
        except Exception:
            pass


def _write_sales_report_sheet(writer, start_date_str: str, end_date_str: str) -> None:
    """Order-level Shopify sales with state (province) and city for the rollup date range."""
    try:
        sales_df = fetch_shopify_sales_orders_detail(start_date_str, end_date_str)
        if sales_df.empty:
            empty = pd.DataFrame(
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
            empty.to_excel(writer, sheet_name="sales_report", index=False)
        else:
            sales_df.to_excel(writer, sheet_name="sales_report", index=False)
    except Exception as e:
        print(f"[DailyRollup] sales_report sheet failed: {e}")
        pd.DataFrame().to_excel(writer, sheet_name="sales_report", index=False)


def run(start_date: str = None, end_date: str = None, out_dir: str = None) -> str:
    """
    Fetch data for the date range, build rollups and outputs, write CSV and Excel.
    Returns path to the Excel written.
    When out_dir is None, uses get_report_dir() (e.g. /tmp/reports in Azure Functions).
    """
    if out_dir is None:
        from global_config import get_report_dir
        out_dir = get_report_dir()
    os.makedirs(out_dir, exist_ok=True)

    try:
        from api_data_fetcher import clear_marketing_cache
        clear_marketing_cache()
    except Exception:
        pass

    # Always resolve timeframe via timeframe_config to honor globals/env and persist
    tf = get_timeframe_config(start_date, end_date)
    s = tf['start_date'].strftime('%Y-%m-%d')
    e = tf['end_date'].strftime('%Y-%m-%d')
    
    # Debug: Show what dates we're using
    print(f"[DailyRollup] Using date range: {s} to {e}")
    print(f"[DailyRollup] Input parameters - start_date: {start_date}, end_date: {end_date}")
    print(f"[DailyRollup] Resolved timeframe - start: {tf['start_date']}, end: {tf['end_date']}, days: {tf.get('days', 'unknown')}")

    # Fetch attribution data
    df = fetch_marketing_hourly(s, e)
    
    if df.empty:
        print(f"[DailyRollup] No data found for date range {s} to {e}")
        # Create empty Excel file
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        base = f"Entity report_{s}_to_{e}_{ts}"
        xlsx_path = os.path.join(out_dir, f"{base}.xlsx")
        with pd.ExcelWriter(xlsx_path, engine='xlsxwriter') as writer:
            pd.DataFrame().to_excel(writer, sheet_name='meta_ads_rollup', index=False)
            pd.DataFrame().to_excel(writer, sheet_name='meta_campaigns', index=False)
            pd.DataFrame().to_excel(writer, sheet_name='google_campaigns', index=False)
            pd.DataFrame().to_excel(writer, sheet_name='organic_campaigns', index=False)
            # pd.DataFrame().to_excel(writer, sheet_name='raw_meta_data', index=False)
            # pd.DataFrame().to_excel(writer, sheet_name='raw_google_data', index=False)
            # pd.DataFrame().to_excel(writer, sheet_name='raw_organic_data', index=False)
            _write_sales_report_sheet(writer, s, e)
            _write_amazon_sp_sheet(writer, s, e)
        return xlsx_path

    # Build rollups
    meta_ads_base = build_meta_ads_rollup(df)
    meta_ads_rollup = build_meta_ads_rollup_with_sku(df)
    meta_campaigns = build_meta_campaigns_rollup(df)
    google_campaigns = build_google_campaigns_rollup_with_sku(df)
    organic_campaigns = build_organic_campaigns_rollup_with_sku(df)

    # TEMP: Inject Google total spend via api_data_fetcher into rows where channel == 'Google'
    google_spend_val = None
    try:
        google_spend_val = float(fetch_google_spend(s, e) or 0)
    except Exception as e:
        google_spend_val = None

    # NOTE: Individual Google campaign spend is already available from the database
    # The _append_spend_to_google_rows function was replacing individual campaign spend 
    # with total API spend, which is not what we want for campaign-wise analysis
    # Individual campaign spend is preserved from the database attribution data

    # File names
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    base = f"Entity report_{s}_to_{e}_{ts}"
    csv_path = os.path.join(out_dir, f"{base}.csv")
    xlsx_path = os.path.join(out_dir, f"{base}.xlsx")

    # Write CSV (meta ads rollup)
    if not meta_ads_rollup.empty:
        round_for_output(meta_ads_rollup).to_csv(csv_path, index=False)
    else:
        pd.DataFrame().to_csv(csv_path, index=False)

    # Write Excel with multiple sheets
    with pd.ExcelWriter(
        xlsx_path,
        engine='xlsxwriter',
        engine_kwargs={'options': {'nan_inf_to_errors': True}}
    ) as writer:
        
        # Meta ads rollup sheet (similar to ad_rollup from dailyrollup_orig.py)
        if not meta_ads_rollup.empty:
            # Sort by net_roas descending, then by hierarchy for proper merging of repeated values
            key_cols_for_merge = ['date_start','channel','campaign_name','adset_name','ad_name']
            sort_cols = []
            if 'net_roas' in meta_ads_rollup.columns:
                sort_cols.append('net_roas')
            sort_cols.extend([c for c in key_cols_for_merge if c in meta_ads_rollup.columns])
            
            # Sort with net_roas descending, then hierarchy ascending
            ascending_flags = [False] + [True] * (len(sort_cols) - 1)
            meta_ads_sorted = meta_ads_rollup.sort_values(sort_cols, ascending=ascending_flags, na_position='last')
            
            # Add Grand Total row with proper aggregation logic
            try:
                # Use ad-level rollup (pre-SKU) for accurate totals that match meta_campaigns
                src = meta_ads_base.copy()
                # Normalize types with better error handling
                numeric_cols = ['impressions','clicks','_landing_page_view','_add_to_cart','_initiate_checkout',
                               'shopify_orders','spend','shopify_revenue','shopify_cogs','total_sku_quantity',
                               'quantity','sku_revenue','sku_cogs']
                for c in numeric_cols:
                    if c in src.columns:
                        src[c] = pd.to_numeric(src[c], errors='coerce').fillna(0)
                
                ad_keys = [c for c in ['date_start','channel','campaign_name','adset_name','ad_name'] if c in src.columns]
                non_sku_cols = [c for c in ['impressions','clicks','_landing_page_view','_add_to_cart','_initiate_checkout',
                                            'shopify_orders','spend','shopify_revenue','shopify_cogs','total_sku_quantity'] if c in src.columns]
                sku_cols = [c for c in ['quantity'] if c in meta_ads_rollup.columns]

                total_map = {}
                for c in non_sku_cols:
                    total_map[c] = float(src[c].sum())
                for c in sku_cols:
                    total_map[c] = float(meta_ads_rollup[c].sum())
                
                # Compute derived totals with proper error handling (matching campaign rollup methodology)
                with np.errstate(divide='ignore', invalid='ignore'):
                    # CTR: Use simple aggregation from total clicks/impressions (same as campaign rollup)
                    if 'clicks' in total_map and 'impressions' in total_map and total_map['impressions'] > 0:
                        total_map['ctr'] = (total_map['clicks'] / total_map['impressions']) * 100
                    else:
                        total_map['ctr'] = 0.0
                    
                    # Bounce Rate: Use simple aggregation with clipping (same as campaign rollup)
                    if 'clicks' in total_map and '_landing_page_view' in total_map and total_map['clicks'] > 0:
                        bounce = ((total_map['clicks'] - total_map['_landing_page_view']) / total_map['clicks']) * 100
                        total_map['bounce_rate'] = max(0, min(100, bounce))  # Clip between 0-100 like campaign rollup
                    else:
                        total_map['bounce_rate'] = 0.0
                    
                    if 'shopify_revenue' in total_map and 'spend' in total_map and total_map['spend'] > 0:
                        total_map['gross_roas'] = total_map['shopify_revenue'] / total_map['spend']
                    else:
                        total_map['gross_roas'] = 0.0
                    
                    if 'shopify_revenue' in total_map and 'shopify_cogs' in total_map and 'spend' in total_map and total_map['spend'] > 0:
                        total_map['net_roas'] = (total_map['shopify_revenue'] - total_map['shopify_cogs']) / total_map['spend']
                        total_map['net_profit'] = total_map['shopify_revenue'] - total_map['shopify_cogs'] - total_map['spend']
                        if total_map['shopify_revenue'] > 0:
                            total_map['profit_margin'] = (total_map['net_profit'] / total_map['shopify_revenue']) * 100
                        else:
                            total_map['profit_margin'] = 0.0
                    else:
                        total_map['net_roas'] = 0.0
                        total_map['net_profit'] = 0.0
                        total_map['profit_margin'] = 0.0
                
                # Replace any inf/nan values with 0
                for key, value in total_map.items():
                    if not np.isfinite(value):
                        total_map[key] = 0.0
                
                # Build total row
                total_row = {c: '' for c in meta_ads_sorted.columns}
                if 'date_start' in total_row: total_row['date_start'] = 'Total'
                if 'channel' in total_row: total_row['channel'] = 'All'
                if 'campaign_name' in total_row: total_row['campaign_name'] = 'Grand Total'
                if 'adset_name' in total_row: total_row['adset_name'] = ''
                if 'ad_name' in total_row: total_row['ad_name'] = ''
                if 'sku' in total_row: total_row['sku'] = ''
                
                # Copy numeric totals with validation (exclude unit_price and unit_cost - leave blank for Grand Total)
                numeric_total_cols = ['impressions','clicks','_landing_page_view','_add_to_cart','_initiate_checkout',
                                     'shopify_orders','spend','shopify_revenue','shopify_cogs','total_sku_quantity',
                                     'quantity','ctr','bounce_rate',
                                     'gross_roas','net_roas','net_profit','profit_margin']
                for c in numeric_total_cols:
                    if c in total_row and c in total_map:
                        total_row[c] = total_map[c]
                
                # Set unit price and cost to empty for Grand Total (per-unit values don't aggregate)
                if 'unit_price' in total_row: total_row['unit_price'] = ''
                if 'unit_cost' in total_row: total_row['unit_cost'] = ''
                
                # Append total row
                meta_ads_sorted = pd.concat([meta_ads_sorted, pd.DataFrame([total_row])], ignore_index=True)

                # Override the Grand Total bounce rate with the real session-weighted
                # value from /v1/meta-funnel (the total_map version above is the fake
                # (clicks - LPV)/clicks ~100). Per-ad rows already carry the real value.
                try:
                    _, _fsum = _fetch_meta_funnel_by_ad(s, e)
                    if 'bounce_rate' in meta_ads_sorted.columns and _fsum and _fsum.get('avg_bounce_rate') is not None:
                        meta_ads_sorted.loc[meta_ads_sorted.index[-1], 'bounce_rate'] = max(
                            0.0, min(100.0, float(_fsum.get('avg_bounce_rate') or 0))
                        )
                except Exception as _bexc:
                    print(f"[Meta Ads Grand Total] Real bounce override skipped: {_bexc}")
            except Exception as e:
                print(f"[Meta Ads Grand Total] Error calculating grand total: {e}")
                # Continue without grand total rather than failing silently
            
            # Round for output
            meta_ads_rounded = round_for_output(meta_ads_sorted)
            
            # Rename headers for presentation (funnel structure)
            rename_map = {
                '_landing_page_view': 'LPV',
                'bounce_rate': 'Bounce Rate',
                '_add_to_cart': 'ATC',
                'unit_price': 'sku_unit_price',
                'unit_cost': 'sku_unit_cogs',
            }
            meta_ads_rounded = meta_ads_rounded.rename(columns={k:v for k,v in rename_map.items() if k in meta_ads_rounded.columns})
            meta_ads_rounded = drop_internal_id_columns(meta_ads_rounded)
            
            # Drop vendor, product_title, variant_title, profit_margin, total_sku_quantity, sku_revenue, sku_cogs, impressions, _landing_page_view, LPV for presentation
            drop_cols = [c for c in ['campaign_status','vendor','product_title','variant_title','profit_margin','total_sku_quantity','sku_revenue','sku_cogs','impressions','_landing_page_view','LPV','bounce_rate','Bounce Rate'] if c in meta_ads_rounded.columns]
            meta_ads_rounded = meta_ads_rounded.drop(columns=drop_cols)
            
            meta_ads_rounded.to_excel(writer, sheet_name='meta_ads_rollup', index=False)
            
            # Apply formatting
            try:
                workbook = writer.book
                center_fmt = workbook.add_format({'align': 'center', 'valign': 'vcenter'})
                header_fmt = workbook.add_format({'bold': True, 'align': 'center', 'valign': 'vcenter', 'bg_color': '#F2F2F2', 'border': 1})
                total_fmt = workbook.add_format({'bold': True, 'bg_color': '#E6F3FF'})
                worksheet = writer.sheets['meta_ads_rollup']
                worksheet.set_column(0, len(meta_ads_rounded.columns)-1, None, center_fmt)
                worksheet.freeze_panes(1, 0)
                worksheet.set_row(0, None, header_fmt)
                
                # Color Grand Total row (last row)
                try:
                    last_row = len(meta_ads_rounded)
                    worksheet.set_row(last_row, None, total_fmt)
                except Exception:
                    pass
                
                # Conditional formatting for profit column if present
                try:
                    if 'net_profit' in meta_ads_rounded.columns:
                        profit_col = meta_ads_rounded.columns.get_loc('net_profit')
                        green_fmt = workbook.add_format({'font_color': '#006100', 'bg_color': '#C6EFCE'})
                        red_fmt = workbook.add_format({'font_color': '#9C0006', 'bg_color': '#FFC7CE'})
                        worksheet.conditional_format(1, profit_col, len(meta_ads_rounded), profit_col, {
                            'type': 'cell', 'criteria': '>', 'value': 0, 'format': green_fmt
                        })
                        worksheet.conditional_format(1, profit_col, len(meta_ads_rounded), profit_col, {
                            'type': 'cell', 'criteria': '<', 'value': 0, 'format': red_fmt
                        })
                except Exception:
                    pass
                
                # Heatmap for Bounce Rate: 0 ignored (white), 100 worst (light blue)
                try:
                    bounce_col_name = 'Bounce Rate' if 'Bounce Rate' in meta_ads_rounded.columns else ('bounce_rate' if 'bounce_rate' in meta_ads_rounded.columns else None)
                    if bounce_col_name is not None:
                        bcol = meta_ads_rounded.columns.get_loc(bounce_col_name)
                        worksheet.conditional_format(1, bcol, len(meta_ads_rounded), bcol, {
                            'type': '2_color_scale',
                            'min_type': 'num', 'min_value': 1, 'min_color': '#FFFFFF',
                            'max_type': 'num', 'max_value': 100, 'max_color': '#D9EAFB'
                        })
                except Exception:
                    pass
                
                # Gradient formatting for CTR column
                try:
                    ctr_col_name = 'ctr' if 'ctr' in meta_ads_rounded.columns else None
                    if ctr_col_name is not None:
                        ctr_col = meta_ads_rounded.columns.get_loc(ctr_col_name)
                        worksheet.conditional_format(1, ctr_col, len(meta_ads_rounded), ctr_col, {
                            'type': '2_color_scale',
                            'min_type': 'num', 'min_value': 0, 'min_color': '#FFFFFF',
                            'max_type': 'num', 'max_value': 5, 'max_color': '#90EE90'
                        })
                except Exception:
                    pass
                
                
                # Gradient formatting for Spend column
                try:
                    spend_col_name = 'spend' if 'spend' in meta_ads_rounded.columns else None
                    if spend_col_name is not None:
                        spend_col = meta_ads_rounded.columns.get_loc(spend_col_name)
                        # Get max spend value for scaling
                        spend_values = pd.to_numeric(meta_ads_rounded[spend_col_name], errors='coerce').fillna(0)
                        max_spend = spend_values.max() if len(spend_values) > 0 else 1000
                        worksheet.conditional_format(1, spend_col, len(meta_ads_rounded), spend_col, {
                            'type': '2_color_scale',
                            'min_type': 'num', 'min_value': 0, 'min_color': '#FFFFFF',
                            'max_type': 'num', 'max_value': max_spend, 'max_color': '#FFFF00'
                        })
                except Exception:
                    pass
                
                # Red formatting for Net ROAS < 1
                try:
                    net_roas_col_name = 'net_roas' if 'net_roas' in meta_ads_rounded.columns else None
                    if net_roas_col_name is not None:
                        net_roas_col = meta_ads_rounded.columns.get_loc(net_roas_col_name)
                        red_fmt = workbook.add_format({'font_color': '#9C0006', 'bg_color': '#FFC7CE'})
                        worksheet.conditional_format(1, net_roas_col, len(meta_ads_rounded), net_roas_col, {
                            'type': 'cell', 'criteria': '<', 'value': 1, 'format': red_fmt
                        })
                except Exception:
                    pass
            except Exception:
                pass
            
            # Merge repeating values for grouped display
            metric_cols_to_merge = [
                'clicks','ctr','ATC','_initiate_checkout',
                'shopify_orders','Bounce Rate','spend','shopify_revenue','shopify_cogs','gross_roas','net_roas','profit'
            ]
            display_merge_cols = []
            for c in key_cols_for_merge + metric_cols_to_merge:
                if c in meta_ads_rounded.columns:
                    display_merge_cols.append(c)
            
            # Don't sum shopify_revenue/cogs across SKU rows (they're ad-level metrics that repeat)
            _merge_repeating_values_in_sheet(
                writer,
                meta_ads_rounded,
                'meta_ads_rollup',
                display_merge_cols,
                scope_columns=[c for c in ['date_start','channel','campaign_name'] if c in meta_ads_rounded.columns],
                sum_columns=[]  # No summing needed since we're showing unit prices per SKU
            )
        else:
            pd.DataFrame().to_excel(writer, sheet_name='meta_ads_rollup', index=False)

        # Meta campaigns sheet
        if not meta_campaigns.empty:
            # Add Grand Total row with proper aggregation logic
            try:
                # Use ad-level rollup totals (matches sum of meta_campaigns rows)
                src = meta_ads_base.copy()
                
                # Normalize types with better error handling
                numeric_cols = ['impressions','clicks','spend','shopify_orders','shopify_revenue','shopify_cogs',
                               '_landing_page_view','_add_to_cart','_initiate_checkout']
                for c in numeric_cols:
                    if c in src.columns:
                        src[c] = pd.to_numeric(src[c], errors='coerce').fillna(0)
                
                # Calculate totals with better precision
                total_map = {}
                for c in numeric_cols:
                    if c in src.columns:
                        total_map[c] = float(src[c].sum())
                
                # Compute derived totals with proper error handling (matching campaign rollup methodology)
                with np.errstate(divide='ignore', invalid='ignore'):
                    # CTR: Use simple aggregation from total clicks/impressions (same as campaign rollup)
                    if 'clicks' in total_map and 'impressions' in total_map and total_map['impressions'] > 0:
                        total_map['ctr'] = (total_map['clicks'] / total_map['impressions']) * 100
                    else:
                        total_map['ctr'] = 0.0
                    
                    if 'shopify_revenue' in total_map and 'spend' in total_map and total_map['spend'] > 0:
                        total_map['gross_roas'] = total_map['shopify_revenue'] / total_map['spend']
                    else:
                        total_map['gross_roas'] = 0.0
                    
                    if 'shopify_revenue' in total_map and 'shopify_cogs' in total_map and 'spend' in total_map and total_map['spend'] > 0:
                        total_map['net_roas'] = (total_map['shopify_revenue'] - total_map['shopify_cogs']) / total_map['spend']
                        total_map['net_profit'] = total_map['shopify_revenue'] - total_map['shopify_cogs'] - total_map['spend']
                        if total_map['shopify_revenue'] > 0:
                            total_map['profit_margin'] = (total_map['net_profit'] / total_map['shopify_revenue']) * 100
                        else:
                            total_map['profit_margin'] = 0.0
                    else:
                        total_map['net_roas'] = 0.0
                        total_map['net_profit'] = 0.0
                        total_map['profit_margin'] = 0.0

                    # Calculate be_roas (breakeven ROAS)
                    if 'shopify_cogs' in total_map and 'spend' in total_map and total_map['spend'] > 0:
                        total_map['be_roas'] = (total_map['shopify_cogs'] + total_map['spend']) / total_map['spend']
                    else:
                        total_map['be_roas'] = 0.0
                    
                    # Calculate conversion_rate (orders / clicks * 100)
                    if 'shopify_orders' in total_map and 'clicks' in total_map and total_map['clicks'] > 0:
                        total_map['conversion_rate'] = (total_map['shopify_orders'] / total_map['clicks']) * 100
                    else:
                        total_map['conversion_rate'] = 0.0
                
                # Replace any inf/nan values with 0
                for key, value in total_map.items():
                    if not np.isfinite(value):
                        total_map[key] = 0.0
                
                # Build total row
                total_row = {c: '' for c in meta_campaigns.columns}
                if 'date_start' in total_row: total_row['date_start'] = 'Total'
                if 'channel' in total_row: total_row['channel'] = 'All'
                if 'campaign_name' in total_row: total_row['campaign_name'] = 'Grand Total'
                
                # Copy numeric totals with validation
                numeric_total_cols = ['impressions','clicks','spend','shopify_orders','shopify_revenue','shopify_cogs',
                                     '_landing_page_view','_add_to_cart','_initiate_checkout','ctr',
                                     'gross_roas','net_roas','be_roas','conversion_rate','net_profit','profit_margin']
                for c in numeric_total_cols:
                    if c in total_row and c in total_map:
                        total_row[c] = total_map[c]
                
                # Append total row
                meta_campaigns = pd.concat([meta_campaigns, pd.DataFrame([total_row])], ignore_index=True)
            except Exception as e:
                print(f"[Meta Campaigns Grand Total] Error calculating grand total: {e}")
                # Continue without grand total rather than failing silently
            
            meta_campaigns_rounded = round_for_output(meta_campaigns)
            meta_campaigns_rounded = drop_internal_id_columns(meta_campaigns_rounded)
            meta_campaigns_rounded.to_excel(writer, sheet_name='meta_campaigns', index=False)
            
            # Apply formatting and color schema
            try:
                workbook = writer.book
                center_fmt = workbook.add_format({'align': 'center', 'valign': 'vcenter'})
                header_fmt = workbook.add_format({'bold': True, 'align': 'center', 'valign': 'vcenter', 'bg_color': '#F2F2F2', 'border': 1})
                total_fmt = workbook.add_format({'bold': True, 'bg_color': '#E6F3FF'})
                worksheet = writer.sheets['meta_campaigns']
                worksheet.set_column(0, len(meta_campaigns_rounded.columns)-1, None, center_fmt)
                worksheet.freeze_panes(1, 0)
                worksheet.set_row(0, None, header_fmt)
                
                # Color Grand Total row (last row)
                try:
                    last_row = len(meta_campaigns_rounded)
                    worksheet.set_row(last_row, None, total_fmt)
                except Exception:
                    pass
                
                # Conditional formatting for profit column if present
                try:
                    if 'net_profit' in meta_campaigns_rounded.columns:
                        profit_col = meta_campaigns_rounded.columns.get_loc('net_profit')
                        green_fmt = workbook.add_format({'font_color': '#006100', 'bg_color': '#C6EFCE'})
                        red_fmt = workbook.add_format({'font_color': '#9C0006', 'bg_color': '#FFC7CE'})
                        worksheet.conditional_format(1, profit_col, len(meta_campaigns_rounded), profit_col, {
                            'type': 'cell', 'criteria': '>', 'value': 0, 'format': green_fmt
                        })
                        worksheet.conditional_format(1, profit_col, len(meta_campaigns_rounded), profit_col, {
                            'type': 'cell', 'criteria': '<', 'value': 0, 'format': red_fmt
                        })
                except Exception:
                    pass
                
                # Gradient formatting for CTR column
                try:
                    if 'ctr' in meta_campaigns_rounded.columns:
                        ctr_col = meta_campaigns_rounded.columns.get_loc('ctr')
                        worksheet.conditional_format(1, ctr_col, len(meta_campaigns_rounded), ctr_col, {
                            'type': '2_color_scale',
                            'min_type': 'num', 'min_value': 0, 'min_color': '#FFFFFF',
                            'max_type': 'num', 'max_value': 5, 'max_color': '#90EE90'
                        })
                except Exception:
                    pass
                
                
                # Gradient formatting for Spend column
                try:
                    if 'spend' in meta_campaigns_rounded.columns:
                        spend_col = meta_campaigns_rounded.columns.get_loc('spend')
                        # Get max spend value for scaling
                        spend_values = pd.to_numeric(meta_campaigns_rounded['spend'], errors='coerce').fillna(0)
                        max_spend = spend_values.max() if len(spend_values) > 0 else 1000
                        worksheet.conditional_format(1, spend_col, len(meta_campaigns_rounded), spend_col, {
                            'type': '2_color_scale',
                            'min_type': 'num', 'min_value': 0, 'min_color': '#FFFFFF',
                            'max_type': 'num', 'max_value': max_spend, 'max_color': '#FFFF00'
                        })
                except Exception:
                    pass
                
                # Red formatting for Net ROAS < 1
                try:
                    if 'net_roas' in meta_campaigns_rounded.columns:
                        net_roas_col = meta_campaigns_rounded.columns.get_loc('net_roas')
                        red_fmt = workbook.add_format({'font_color': '#9C0006', 'bg_color': '#FFC7CE'})
                        worksheet.conditional_format(1, net_roas_col, len(meta_campaigns_rounded), net_roas_col, {
                            'type': 'cell', 'criteria': '<', 'value': 1, 'format': red_fmt
                        })
                except Exception:
                    pass
            except Exception:
                pass
        else:
            pd.DataFrame().to_excel(writer, sheet_name='meta_campaigns', index=False)

        # Google campaigns sheet
        if not google_campaigns.empty:
            # Apply column ordering and remove unwanted columns (same as meta ads rollup)
            google_campaigns = google_campaigns[order_columns_by_funnel(google_campaigns, include_sku=True)]
            
            # Rename unit columns to sku_unit_price and sku_unit_cogs
            google_campaigns = google_campaigns.rename(columns={
                'unit_price': 'sku_unit_price',
                'unit_cost': 'sku_unit_cogs'
            })
            
            # Drop vendor, product_title, variant_title, profit_margin, total_sku_quantity, sku_revenue, sku_cogs for presentation
            drop_cols = [c for c in [
                'vendor', 'product_title', 'variant_title', 'profit_margin', 'total_sku_quantity',
                'sku_revenue', 'sku_cogs', 'sku_unit_price', 'sku_unit_cogs', 'sku_quantity',
            ] if c in google_campaigns.columns]
            google_campaigns = google_campaigns.drop(columns=drop_cols)
            google_campaigns = drop_internal_id_columns(google_campaigns)
            
            # Add Grand Total row with proper aggregation logic
            try:
                # Use raw data for accurate totals (not the processed campaign data)
                # Get the raw Google data that was used to build the campaigns
                google_raw = df[df['source'] == 'Google Ads'].copy()
                if google_raw.empty:
                    # Fallback to campaign data if raw data not available
                    src = google_campaigns.copy()
                else:
                    # Transform the raw data to get the same column structure
                    src = transform_attribution_data(google_raw)
                
                # Normalize types with better error handling
                numeric_cols = ['impressions','clicks','spend','shopify_orders','shopify_revenue','shopify_cogs',
                               '_landing_page_view','_add_to_cart','_initiate_checkout']
                for c in numeric_cols:
                    if c in src.columns:
                        src[c] = pd.to_numeric(src[c], errors='coerce').fillna(0)
                
                # Calculate totals with better precision (same approach as Meta campaigns)
                total_map = {}
                for c in numeric_cols:
                    if c in src.columns:
                        total_map[c] = float(src[c].sum())
                
                # Compute derived totals with proper error handling (matching campaign rollup methodology)
                with np.errstate(divide='ignore', invalid='ignore'):
                    # CTR: Use simple aggregation from total clicks/impressions (same as campaign rollup)
                    if 'clicks' in total_map and 'impressions' in total_map and total_map['impressions'] > 0:
                        total_map['ctr'] = (total_map['clicks'] / total_map['impressions']) * 100
                    else:
                        total_map['ctr'] = 0.0
                    
                    # Bounce Rate: Use simple aggregation with clipping (same as campaign rollup)
                    if 'clicks' in total_map and '_landing_page_view' in total_map and total_map['clicks'] > 0:
                        bounce = ((total_map['clicks'] - total_map['_landing_page_view']) / total_map['clicks']) * 100
                        total_map['bounce_rate'] = max(0, min(100, bounce))  # Clip between 0-100 like campaign rollup
                    else:
                        total_map['bounce_rate'] = 0.0
                    
                    # Gross ROAS
                    if 'shopify_revenue' in total_map and 'spend' in total_map and total_map['spend'] > 0:
                        total_map['gross_roas'] = total_map['shopify_revenue'] / total_map['spend']
                    else:
                        total_map['gross_roas'] = 0.0
                    
                    # Net ROAS and Profit
                    if 'shopify_revenue' in total_map and 'shopify_cogs' in total_map and 'spend' in total_map and total_map['spend'] > 0:
                        total_map['net_roas'] = (total_map['shopify_revenue'] - total_map['shopify_cogs']) / total_map['spend']
                        total_map['net_profit'] = total_map['shopify_revenue'] - total_map['shopify_cogs'] - total_map['spend']
                        if total_map['shopify_revenue'] > 0:
                            total_map['profit_margin'] = (total_map['net_profit'] / total_map['shopify_revenue']) * 100
                        else:
                            total_map['profit_margin'] = 0.0
                    else:
                        total_map['net_roas'] = 0.0
                        total_map['net_profit'] = 0.0
                        total_map['profit_margin'] = 0.0

                    # BE ROAS
                    if 'shopify_cogs' in total_map and 'spend' in total_map and total_map['spend'] > 0:
                        total_map['be_roas'] = (total_map['shopify_cogs'] + total_map['spend']) / total_map['spend']
                    else:
                        total_map['be_roas'] = 0.0
                    
                    # Conversion rate
                    if 'shopify_orders' in total_map and 'clicks' in total_map and total_map['clicks'] > 0:
                        total_map['conversion_rate'] = (total_map['shopify_orders'] / total_map['clicks']) * 100
                    else:
                        total_map['conversion_rate'] = 0.0
                
                # Replace any inf/nan values with 0
                for key, value in total_map.items():
                    if not np.isfinite(value):
                        total_map[key] = 0.0
                
                # Build total row
                total_row = {c: '' for c in google_campaigns.columns}
                if 'date_start' in total_row: total_row['date_start'] = 'Total'
                if 'channel' in total_row: total_row['channel'] = 'All'
                if 'campaign_name' in total_row: total_row['campaign_name'] = 'Grand Total'
                if 'sku' in total_row: total_row['sku'] = ''
                
                # Copy numeric totals with validation (exclude unit_price and unit_cost - leave blank for Grand Total)
                numeric_total_cols = ['impressions','clicks','spend','shopify_orders','shopify_revenue','shopify_cogs',
                                     '_landing_page_view','_add_to_cart','_initiate_checkout','ctr','bounce_rate',
                                     'gross_roas','net_roas','be_roas','conversion_rate','net_profit','profit_margin',
                                     'quantity','sku_quantity']
                for c in numeric_total_cols:
                    if c in total_row and c in total_map:
                        total_row[c] = total_map[c]
                
                # Set unit price and cost to empty for Grand Total (per-unit values don't aggregate)
                if 'unit_price' in total_row: total_row['unit_price'] = ''
                if 'unit_cost' in total_row: total_row['unit_cost'] = ''
                if 'sku_unit_price' in total_row: total_row['sku_unit_price'] = ''
                if 'sku_unit_cogs' in total_row: total_row['sku_unit_cogs'] = ''
                
                # Append total row
                google_campaigns = pd.concat([google_campaigns, pd.DataFrame([total_row])], ignore_index=True)
            except Exception:
                pass
            
            google_campaigns_rounded = round_for_output(google_campaigns)
            google_campaigns_rounded.to_excel(writer, sheet_name='google_campaigns', index=False)
            
            # Merge repeating values for better visual grouping
            try:
                display_merge_cols = []
                # Basic grouping columns
                for c in ['date_start','channel','campaign_name']:
                    if c in google_campaigns_rounded.columns:
                        display_merge_cols.append(c)
                
                # Campaign-level metrics that should be merged across SKU rows
                campaign_metrics = ['ctr','spend','shopify_revenue','shopify_cogs','shopify_orders','gross_roas','net_roas','net_profit','be_roas','conversion_rate']
                for c in campaign_metrics:
                    if c in google_campaigns_rounded.columns:
                        display_merge_cols.append(c)
                
                _merge_repeating_values_in_sheet(
                    writer,
                    google_campaigns_rounded,
                    'google_campaigns',
                    display_merge_cols
                )
            except Exception:
                pass
            
            # Apply formatting and color schema
            try:
                workbook = writer.book
                center_fmt = workbook.add_format({'align': 'center', 'valign': 'vcenter'})
                header_fmt = workbook.add_format({'bold': True, 'align': 'center', 'valign': 'vcenter', 'bg_color': '#F2F2F2', 'border': 1})
                total_fmt = workbook.add_format({'bold': True, 'bg_color': '#E6F3FF'})
                worksheet = writer.sheets['google_campaigns']
                worksheet.set_column(0, len(google_campaigns_rounded.columns)-1, None, center_fmt)
                worksheet.freeze_panes(1, 0)
                worksheet.set_row(0, None, header_fmt)
                
                # Color Grand Total row (last row)
                try:
                    last_row = len(google_campaigns_rounded)
                    worksheet.set_row(last_row, None, total_fmt)
                except Exception:
                    pass
                
                # Conditional formatting for profit column if present
                try:
                    if 'net_profit' in google_campaigns_rounded.columns:
                        profit_col = google_campaigns_rounded.columns.get_loc('net_profit')
                        green_fmt = workbook.add_format({'font_color': '#006100', 'bg_color': '#C6EFCE'})
                        red_fmt = workbook.add_format({'font_color': '#9C0006', 'bg_color': '#FFC7CE'})
                        worksheet.conditional_format(1, profit_col, len(google_campaigns_rounded), profit_col, {
                            'type': 'cell', 'criteria': '>', 'value': 0, 'format': green_fmt
                        })
                        worksheet.conditional_format(1, profit_col, len(google_campaigns_rounded), profit_col, {
                            'type': 'cell', 'criteria': '<', 'value': 0, 'format': red_fmt
                        })
                except Exception:
                    pass
                
                # Heatmap for Bounce Rate
                try:
                    if 'bounce_rate' in google_campaigns_rounded.columns:
                        bounce_col = google_campaigns_rounded.columns.get_loc('bounce_rate')
                        worksheet.conditional_format(1, bounce_col, len(google_campaigns_rounded), bounce_col, {
                            'type': '2_color_scale',
                            'min_type': 'num', 'min_value': 1, 'min_color': '#FFFFFF',
                            'max_type': 'num', 'max_value': 100, 'max_color': '#D9EAFB'
                        })
                except Exception:
                    pass
                
                # Gradient formatting for CTR column
                try:
                    if 'ctr' in google_campaigns_rounded.columns:
                        ctr_col = google_campaigns_rounded.columns.get_loc('ctr')
                        worksheet.conditional_format(1, ctr_col, len(google_campaigns_rounded), ctr_col, {
                            'type': '2_color_scale',
                            'min_type': 'num', 'min_value': 0, 'min_color': '#FFFFFF',
                            'max_type': 'num', 'max_value': 5, 'max_color': '#90EE90'
                        })
                except Exception:
                    pass
                
                
                # Gradient formatting for Spend column
                try:
                    if 'spend' in google_campaigns_rounded.columns:
                        spend_col = google_campaigns_rounded.columns.get_loc('spend')
                        # Get max spend value for scaling
                        spend_values = pd.to_numeric(google_campaigns_rounded['spend'], errors='coerce').fillna(0)
                        max_spend = spend_values.max() if len(spend_values) > 0 else 1000
                        worksheet.conditional_format(1, spend_col, len(google_campaigns_rounded), spend_col, {
                            'type': '2_color_scale',
                            'min_type': 'num', 'min_value': 0, 'min_color': '#FFFFFF',
                            'max_type': 'num', 'max_value': max_spend, 'max_color': '#FFFF00'
                        })
                except Exception:
                    pass
                
                # Red formatting for Net ROAS < 1
                try:
                    if 'net_roas' in google_campaigns_rounded.columns:
                        net_roas_col = google_campaigns_rounded.columns.get_loc('net_roas')
                        red_fmt = workbook.add_format({'font_color': '#9C0006', 'bg_color': '#FFC7CE'})
                        worksheet.conditional_format(1, net_roas_col, len(google_campaigns_rounded), net_roas_col, {
                            'type': 'cell', 'criteria': '<', 'value': 1, 'format': red_fmt
                        })
                except Exception:
                    pass
            except Exception:
                pass
        else:
            pd.DataFrame().to_excel(writer, sheet_name='google_campaigns', index=False)

        # Organic campaigns sheet
        if not organic_campaigns.empty:
            # Apply column ordering and remove unwanted columns (same as meta ads rollup)
            organic_campaigns = organic_campaigns[order_columns_by_funnel(organic_campaigns, include_sku=True)]
            
            # Drop unwanted columns for organic sheet presentation
            drop_cols = [c for c in [
                'campaign_name', 'shopify_revenue', 'shopify_cogs',
                'vendor', 'product_title', 'variant_title', 'profit_margin', 'total_sku_quantity', 'unit_price', 'unit_cost',
            ] if c in organic_campaigns.columns]
            organic_campaigns = organic_campaigns.drop(columns=drop_cols)
            organic_campaigns = drop_internal_id_columns(organic_campaigns)
            
            # Add Grand Total row with proper aggregation logic
            try:
                # Use original data for accurate totals
                src = organic_campaigns.copy()
                # Normalize types
                for c in ['impressions','clicks','spend','shopify_orders','shopify_revenue','shopify_cogs',
                          '_landing_page_view','_add_to_cart','_initiate_checkout']:
                    if c in src.columns:
                        src[c] = pd.to_numeric(src[c], errors='coerce').fillna(0)
                
                # Calculate totals - need to handle SKU-level metrics properly
                # For Organic campaigns, we need to sum SKU metrics from all rows but avoid double-counting campaign-level metrics
                campaign_keys = ['date_start','channel','campaign_name']
                present_campaign_keys = [c for c in campaign_keys if c in src.columns]
                
                # Sum non-SKU metrics from unique campaign rows to avoid double-counting
                dedup_campaigns = src.drop_duplicates(subset=present_campaign_keys) if present_campaign_keys else src
                non_sku_cols = [c for c in ['impressions','clicks','_landing_page_view','_add_to_cart','_initiate_checkout',
                                            'shopify_orders','spend','shopify_revenue','shopify_cogs'] if c in src.columns]
                sku_cols = [c for c in ['sku_quantity','sku_revenue','sku_cogs'] if c in src.columns]
                
                total_map = {c: float(dedup_campaigns[c].sum()) for c in non_sku_cols}
                total_map.update({c: float(src[c].sum()) for c in sku_cols})
                
                # Compute derived totals (exclude ctr, net_roas, be_roas, conversion_rate, net_profit, gross_roas for organic)
                
                # Build total row
                total_row = {c: '' for c in organic_campaigns.columns}
                if 'date_start' in total_row: total_row['date_start'] = 'Total'
                if 'channel' in total_row: total_row['channel'] = 'All'
                if 'sku' in total_row: total_row['sku'] = ''
                
                # Copy numeric totals (exclude removed columns: campaign_name, shopify_revenue, shopify_cogs)
                for c in ['impressions','clicks','shopify_orders',
                          '_landing_page_view','_add_to_cart','_initiate_checkout','bounce_rate',
                          'sku_quantity','sku_revenue','sku_cogs']:
                    if c in total_row and c in total_map:
                        total_row[c] = float(total_map[c])
                
                # Append total row
                organic_campaigns = pd.concat([organic_campaigns, pd.DataFrame([total_row])], ignore_index=True)
            except Exception:
                pass
            
            organic_campaigns_rounded = round_for_output(organic_campaigns)
            organic_campaigns_rounded.to_excel(writer, sheet_name='organic_campaigns', index=False)
            
            # For organic campaigns, use terminal-like format (no merging for cleaner display)
            # Skip the merging logic for organic data to show individual SKU rows clearly
            
            # Apply terminal-like formatting for organic campaigns
            try:
                workbook = writer.book
                # Terminal-like formatting: left-aligned text, right-aligned numbers
                text_fmt = workbook.add_format({'align': 'left', 'valign': 'vcenter'})
                number_fmt = workbook.add_format({'align': 'right', 'valign': 'vcenter', 'num_format': '#,##0.00'})
                header_fmt = workbook.add_format({'bold': True, 'align': 'center', 'valign': 'vcenter', 'bg_color': '#F2F2F2', 'border': 1})
                total_fmt = workbook.add_format({'bold': True, 'bg_color': '#E6F3FF', 'align': 'right', 'num_format': '#,##0.00'})
                
                worksheet = writer.sheets['organic_campaigns']
                worksheet.freeze_panes(1, 0)
                worksheet.set_row(0, None, header_fmt)
                
                # Set column-specific formatting for terminal-like appearance
                for col_idx, col_name in enumerate(organic_campaigns_rounded.columns):
                    if col_name in ['date_start', 'channel', 'sku', 'vendor', 'product_title', 'variant_title']:
                        worksheet.set_column(col_idx, col_idx, None, text_fmt)
                    else:
                        worksheet.set_column(col_idx, col_idx, None, number_fmt)
                
                # Color Grand Total row (last row)
                try:
                    last_row = len(organic_campaigns_rounded)
                    worksheet.set_row(last_row, None, total_fmt)
                except Exception:
                    pass
                
                # No conditional formatting for profit column in organic campaigns (net_profit removed)
                
                # Heatmap for Bounce Rate
                try:
                    if 'bounce_rate' in organic_campaigns_rounded.columns:
                        bounce_col = organic_campaigns_rounded.columns.get_loc('bounce_rate')
                        worksheet.conditional_format(1, bounce_col, len(organic_campaigns_rounded), bounce_col, {
                            'type': '2_color_scale',
                            'min_type': 'num', 'min_value': 1, 'min_color': '#FFFFFF',
                            'max_type': 'num', 'max_value': 100, 'max_color': '#D9EAFB'
                        })
                except Exception:
                    pass
                
                # Gradient formatting for CTR column
                try:
                    if 'ctr' in organic_campaigns_rounded.columns:
                        ctr_col = organic_campaigns_rounded.columns.get_loc('ctr')
                        worksheet.conditional_format(1, ctr_col, len(organic_campaigns_rounded), ctr_col, {
                            'type': '2_color_scale',
                            'min_type': 'num', 'min_value': 0, 'min_color': '#FFFFFF',
                            'max_type': 'num', 'max_value': 5, 'max_color': '#90EE90'
                        })
                except Exception:
                    pass
                
                
                # Gradient formatting for Spend column
                try:
                    if 'spend' in organic_campaigns_rounded.columns:
                        spend_col = organic_campaigns_rounded.columns.get_loc('spend')
                        # Get max spend value for scaling
                        spend_values = pd.to_numeric(organic_campaigns_rounded['spend'], errors='coerce').fillna(0)
                        max_spend = spend_values.max() if len(spend_values) > 0 else 1000
                        worksheet.conditional_format(1, spend_col, len(organic_campaigns_rounded), spend_col, {
                            'type': '2_color_scale',
                            'min_type': 'num', 'min_value': 0, 'min_color': '#FFFFFF',
                            'max_type': 'num', 'max_value': max_spend, 'max_color': '#FFFF00'
                        })
                except Exception:
                    pass
                
                # Red formatting for Net ROAS < 1
                try:
                    if 'net_roas' in organic_campaigns_rounded.columns:
                        net_roas_col = organic_campaigns_rounded.columns.get_loc('net_roas')
                        red_fmt = workbook.add_format({'font_color': '#9C0006', 'bg_color': '#FFC7CE'})
                        worksheet.conditional_format(1, net_roas_col, len(organic_campaigns_rounded), net_roas_col, {
                            'type': 'cell', 'criteria': '<', 'value': 1, 'format': red_fmt
                        })
                except Exception:
                    pass
            except Exception:
                pass
        else:
            pd.DataFrame().to_excel(writer, sheet_name='organic_campaigns', index=False)

        # Raw data sheets for temporary viewing - COMMENTED OUT
        # # Raw Meta data
        # meta_raw = df[df['source'] == 'Meta Ads'].copy()
        # if not meta_raw.empty:
        #     meta_raw.to_excel(writer, sheet_name='raw_meta_data', index=False)
        # else:
        #     pd.DataFrame().to_excel(writer, sheet_name='raw_meta_data', index=False)

        # # Raw Google data
        # google_raw = df[df['source'] == 'Google Ads'].copy()
        # if not google_raw.empty:
        #     google_raw.to_excel(writer, sheet_name='raw_google_data', index=False)
        # else:
        #     pd.DataFrame().to_excel(writer, sheet_name='raw_google_data', index=False)

        # # Raw Organic data
        # organic_raw = df[df['source'] == 'Organic'].copy()
        # if not organic_raw.empty:
        #     organic_raw.to_excel(writer, sheet_name='raw_organic_data', index=False)
        # else:
        #     pd.DataFrame().to_excel(writer, sheet_name='raw_organic_data', index=False)

        _write_sales_report_sheet(writer, s, e)
        _write_amazon_sp_sheet(writer, s, e)

        # Clean, attribution-sourced entity sheets (API-only, gold-backed).
        # Gated so it can run alongside the legacy PG-fed sheets for A/B
        # comparison; enable with ENTITY_REPORT_CLEAN=true. See entity_report.py.
        if os.getenv("ENTITY_REPORT_CLEAN", "false").lower() in ("1", "true", "yes"):
            try:
                from entity_report import add_entity_sheets
                add_entity_sheets(writer, s, e, round_for_output, sheet_prefix="clean_")
            except Exception as _entity_exc:
                logger.warning("Clean entity sheets skipped: %s", _entity_exc)

    return xlsx_path

if __name__ == '__main__':
    # Centralized timeframe with date debugging
    env_start = os.environ.get('ROLLUP_START_DATE')
    env_end = os.environ.get('ROLLUP_END_DATE')
    print(f"[DateDebug] env ROLLUP_START_DATE={env_start} ROLLUP_END_DATE={env_end}")
    
    # Test what get_timeframe_config() returns without parameters
    tf_test = get_timeframe_config()
    print(f"[DateDebug] get_timeframe_config() without params returns: start={tf_test['start_date']}, end={tf_test['end_date']}")
    
    # Use global timeframe configuration by calling run() without parameters
    # This will make run() call get_timeframe_config() without arguments, 
    # which will use the global dates we set in timeframe_config.py
    print(f"[DateDebug] Calling run() without parameters to use global dates")
    path = run()
    print(f"Wrote outputs to: {path}")