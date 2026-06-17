import requests
import pandas as pd
import sqlalchemy
from datetime import datetime
import os
import logging
from dotenv import load_dotenv
from database_manager import get_db_engine
load_dotenv()

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def get_google_ads_credentials_from_db():
    """
    Fetch Google Ads credentials from the env_config table in the database.
    Returns a dict with keys: client_id, client_secret, refresh_token, developer_token, login_customer_id, customer_id
    """
    engine = get_db_engine()
    query = """
        SELECT key_name, key_value FROM env_config WHERE key_name IN (
            'TH_GOOGLE_CLIENT_ID',
            'TH_GOOGLE_CLIENT_SECRET',
            'TH_GOOGLE_REFRESH_TOKEN',
            'Manager_GOOGLE_ADS_DEVELOPER_TOKEN',
            'TH_GOOGLE_ADS_LOGIN_CUSTOMER_ID',
            'TH_GOOGLE_ADS_CUSTOMER_ID'
        )
    """
    creds = {}
    with engine.connect() as conn:
        result = conn.execute(sqlalchemy.text(query))
        for row in result.mappings():
            creds[row['key_name']] = row['key_value']
    return {
        'client_id': creds.get('TH_GOOGLE_CLIENT_ID'),
        'client_secret': creds.get('TH_GOOGLE_CLIENT_SECRET'),
        'refresh_token': creds.get('TH_GOOGLE_REFRESH_TOKEN'),
        'developer_token': creds.get('Manager_GOOGLE_ADS_DEVELOPER_TOKEN'),
        'login_customer_id': creds.get('TH_GOOGLE_ADS_LOGIN_CUSTOMER_ID'),
        'customer_id': creds.get('TH_GOOGLE_ADS_CUSTOMER_ID'),
    }

# ------------------------
# CONFIG
# ------------------------

def get_google_ads_access_token():
    """
    Fetch a new Google Ads access token using the refresh token flow, using DB credentials.
    """
    creds = get_google_ads_credentials_from_db()
    client_id = creds['client_id']
    client_secret = creds['client_secret']
    refresh_token = creds['refresh_token']
    if not client_id:
        logger.error("GOOGLE_ADS_CLIENT_ID not found in DB")
        raise ValueError("GOOGLE_ADS_CLIENT_ID not found in DB")
    if not client_secret:
        logger.error("GOOGLE_ADS_CLIENT_SECRET not found in DB")
        raise ValueError("GOOGLE_ADS_CLIENT_SECRET not found in DB")
    if not refresh_token:
        logger.error("GOOGLE_ADS_REFRESH_TOKEN not found in DB")
        raise ValueError("GOOGLE_ADS_REFRESH_TOKEN not found in DB")
    token_url = 'https://oauth2.googleapis.com/token'
    payload = {
        'client_id': client_id,
        'client_secret': client_secret,
        'grant_type': 'refresh_token',
        'refresh_token': refresh_token,
    }
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    try:
        logger.info("Requesting Google Ads access token...")
        response = requests.post(token_url, data=payload, headers=headers)
        if response.status_code != 200:
            logger.error(f"Google Ads token request failed with status {response.status_code}")
            logger.error(f"Response content: {response.text}")
            response.raise_for_status()
        token_data = response.json()
        if 'access_token' not in token_data:
            logger.error(f"Access token not found in response: {token_data}")
            raise ValueError("Access token not found in response")
        logger.info("Successfully obtained Google Ads access token")
        return token_data['access_token']
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to get Google Ads access token: {str(e)}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error getting Google Ads access token: {str(e)}")
        raise

def get_google_ads_total_spend():   
    """
    Fetches Google Ads data from the API and returns the total spend (cost_micros) divided by 1,000,000.
    """
    try:
        ACCESS_TOKEN = get_google_ads_access_token()
        creds = get_google_ads_credentials_from_db()
        developer_token = creds['developer_token']
        login_customer_id = creds['login_customer_id']
        customer_id = creds['customer_id']
        if not developer_token:
            logger.error("GOOGLE_ADS_DEVELOPER_TOKEN not found in DB")
            return 0
        if not login_customer_id:
            logger.error("GOOGLE_ADS_LOGIN_CUSTOMER_ID not found in DB")
            return 0
        if not customer_id:
            logger.error("GOOGLE_ADS_CUSTOMER_ID not found in DB")
            return 0
        
        query = """
            SELECT campaign.name,
                   campaign.resource_name,
                   campaign.status,
                   campaign.optimization_score,
                   campaign.advertising_channel_type,
                   campaign.bidding_strategy_type,
                   metrics.clicks,
                   metrics.impressions,
                   metrics.ctr,
                   metrics.average_cpc,
                   metrics.cost_micros,
                   metrics.cost_per_conversion,
                   metrics.conversions_value,
                   metrics.conversions_from_interactions_rate,
                   campaign_budget.resource_name,
                   campaign_budget.amount_micros
            FROM campaign
            WHERE segments.date DURING TODAY
              AND campaign.status != REMOVED
            LIMIT 100
        """
        endpoint = f"https://googleads.googleapis.com/v24/customers/{customer_id}/googleAds:search"
        headers = {
            "Content-Type": "application/json",
            "developer-token": developer_token,
            "login-customer-id": login_customer_id,
            "Authorization": f"Bearer {ACCESS_TOKEN}"
        }
        payload = {"query": query}
        
        logger.info("Requesting Google Ads data...")
        response = requests.post(endpoint, headers=headers, json=payload)
        
        if response.status_code != 200:
            logger.error(f"Google Ads API request failed with status {response.status_code}")
            logger.error(f"Response content: {response.text}")
            return 0
        
        data = response.json()
        records = []
        for r in data.get("results", []):
            metrics = r["metrics"]
            record = {
                "cost_micros": int(metrics.get("costMicros", 0)),
            }
            records.append(record)
        
        df = pd.DataFrame(records)
        api_total_cost_micros = df["cost_micros"].sum() if not df.empty else 0
        api_total_spend_effective = api_total_cost_micros / 1000000
        
        logger.info(f"Google Ads total spend: {api_total_spend_effective}")
        return api_total_spend_effective
        
    except Exception as e:
        logger.error(f"Error getting Google Ads total spend: {str(e)}")
        return 0

def get_th_account_metrics(date=None):
    """
    Fetches TH account metrics from Google Ads API and returns CPP, CTR, CR, impressions, and clicks.
    Returns a dictionary with the calculated metrics.
    
    Args:
        date (str, optional): Date in 'YYYY-MM-DD' format. If None, uses today's date.
    """
    try:
        # Use provided date or default to today
        if date is None:
            from datetime import datetime
            date = datetime.now().strftime('%Y-%m-%d')
        
        ACCESS_TOKEN = get_google_ads_access_token()
        creds = get_google_ads_credentials_from_db()
        developer_token = creds['developer_token']
        login_customer_id = creds['login_customer_id']
        customer_id = creds['customer_id']
        
        if not developer_token:
            logger.error("GOOGLE_ADS_DEVELOPER_TOKEN not found in DB")
            return {}
        if not login_customer_id:
            logger.error("GOOGLE_ADS_LOGIN_CUSTOMER_ID not found in DB")
            return {}
        if not customer_id:
            logger.error("GOOGLE_ADS_CUSTOMER_ID not found in DB")
            return {}
        
        query = f"""
            SELECT segments.date,
                   metrics.impressions,
                   metrics.interactions,
                   metrics.clicks,
                   metrics.ctr,
                   metrics.cost_per_conversion,
                   metrics.conversions
            FROM customer 
            WHERE segments.date = '{date}'
        """
        
        endpoint = f"https://googleads.googleapis.com/v24/customers/{customer_id}/googleAds:search"
        headers = {
            "Content-Type": "application/json",
            "developer-token": developer_token,
            "login-customer-id": login_customer_id,
            "Authorization": f"Bearer {ACCESS_TOKEN}"
        }
        payload = {"query": query}
        
        logger.info("Requesting TH account metrics from Google Ads...")
        response = requests.post(endpoint, headers=headers, json=payload)
        
        if response.status_code != 200:
            logger.error(f"Google Ads API request failed with status {response.status_code}")
            logger.error(f"Response content: {response.text}")
            return {}
        
        data = response.json()
        results = data.get("results", [])
        
        if not results:
            logger.warning("No results found for TH account metrics")
            return {}
        
        # Get the first result (should be the only one for a specific date)
        result = results[0]
        metrics = result.get("metrics", {})
        
        # Extract raw metrics
        impressions = int(metrics.get("impressions", 0))
        interactions = int(metrics.get("interactions", 0))
        clicks = int(metrics.get("clicks", 0))
        conversions = int(metrics.get("conversions", 0))
        ctr = float(metrics.get("ctr", 0))
        cost_per_conversion = float(metrics.get("costPerConversion", 0))
        
        # Calculate CPP (Cost Per Purchase) - divide by 1,000,000 to get real value
        cpp = (cost_per_conversion / 1000000) if cost_per_conversion > 0 else 0
        
        # CTR is already provided by API
        ctr_percentage = ctr * 100  # Convert to percentage
        
        # Calculate CR (Conversion Rate) = (conversions/interactions) * 100
        cr_percentage = 0
        if interactions > 0:
            cr_percentage = (conversions / interactions) * 100
        
        metrics_dict = {
            "cpp": cpp,
            "ctr": ctr_percentage,
            "cr": cr_percentage,
            "impressions": impressions,
            "clicks": clicks,
            "conversions": conversions,
            "interactions": interactions
        }
        
        logger.info(f"TH Account Metrics - CPP: {cpp}, CTR: {ctr_percentage}%, CR: {cr_percentage}%, Impressions: {impressions}, Clicks: {clicks}, Conversions: {conversions}")
        return metrics_dict
        
    except Exception as e:
        logger.error(f"Error getting TH account metrics: {str(e)}")
        return {}

# Load secrets from environment variables
DEVELOPER_TOKEN = os.getenv('GOOGLE_ADS_DEVELOPER_TOKEN')
LOGIN_CUSTOMER_ID = os.getenv('GOOGLE_ADS_LOGIN_CUSTOMER_ID')
CUSTOMER_ID = os.getenv('GOOGLE_ADS_CUSTOMER_ID')

# Your Azure seleric_db connection string
seleric_db_URI = os.getenv('DATABASE_URL')

# Table name
TABLE_NAME = "google_ads_campaigns"

# ------------------------
# EXTRACT FROM GOOGLE ADS
# ------------------------

def extract_google_ads_data():
    """
    Extract Google Ads data and return as DataFrame.
    This function should be called when you actually need the data.
    """
    try:
        # Get a fresh access token for each run
        ACCESS_TOKEN = get_google_ads_access_token()
        
        # Validate required environment variables
        developer_token = os.getenv('GOOGLE_ADS_DEVELOPER_TOKEN')
        login_customer_id = os.getenv('GOOGLE_ADS_LOGIN_CUSTOMER_ID')
        customer_id = os.getenv('GOOGLE_ADS_CUSTOMER_ID')
        
        if not developer_token:
            logger.error("GOOGLE_ADS_DEVELOPER_TOKEN environment variable is not set")
            return pd.DataFrame()
        
        if not login_customer_id:
            logger.error("GOOGLE_ADS_LOGIN_CUSTOMER_ID environment variable is not set")
            return pd.DataFrame()
        
        if not customer_id:
            logger.error("GOOGLE_ADS_CUSTOMER_ID environment variable is not set")
            return pd.DataFrame()
        
        query = """
            SELECT campaign.name,
                   campaign.resource_name,
                   campaign.status,
                   campaign.optimization_score,
                   campaign.advertising_channel_type,
                   campaign.bidding_strategy_type,
                   metrics.clicks,
                   metrics.impressions,
                   metrics.ctr,
                   metrics.average_cpc,
                   metrics.cost_micros,
                   metrics.cost_per_conversion,
                   metrics.conversions_value,
                   metrics.conversions_from_interactions_rate,
                   campaign_budget.resource_name,
                   campaign_budget.amount_micros
            FROM campaign
            WHERE segments.date DURING TODAY
              AND campaign.status != REMOVED
            LIMIT 100
        """
        
        endpoint = f"https://googleads.googleapis.com/v24/customers/{customer_id}/googleAds:search"
        
        headers = {
            "Content-Type": "application/json",
            "developer-token": developer_token,
            "login-customer-id": login_customer_id,
            "Authorization": f"Bearer {ACCESS_TOKEN}"
        }
        
        payload = {"query": query}
        
        logger.info("Requesting Google Ads data...")
        response = requests.post(endpoint, headers=headers, json=payload)
        
        if response.status_code != 200:
            logger.error(f"Google Ads API request failed with status {response.status_code}")
            logger.error(f"Response content: {response.text}")
            return pd.DataFrame()
        
        data = response.json()
        records = []
        for r in data.get("results", []):
            campaign = r.get("campaign", {})
            metrics = r.get("metrics", {})
            campaign_budget = r.get("campaignBudget", {})
            
            record = {
                "campaign_name": campaign.get("name", ""),
                "campaign_resource_name": campaign.get("resourceName", ""),
                "campaign_status": campaign.get("status", ""),
                "optimization_score": campaign.get("optimizationScore", 0),
                "advertising_channel_type": campaign.get("advertisingChannelType", ""),
                "bidding_strategy_type": campaign.get("biddingStrategyType", ""),
                "clicks": int(metrics.get("clicks", 0)),
                "impressions": int(metrics.get("impressions", 0)),
                "ctr": float(metrics.get("ctr", 0)),
                "average_cpc": int(metrics.get("averageCpc", 0)),
                "cost_micros": int(metrics.get("costMicros", 0)),
                "cost_per_conversion": float(metrics.get("costPerConversion", 0)),
                "conversions_value": float(metrics.get("conversionsValue", 0)),
                "conversions_from_interactions_rate": float(metrics.get("conversionsFromInteractionsRate", 0)),
                "campaign_budget_resource_name": campaign_budget.get("resourceName", ""),
                "campaign_budget_amount_micros": int(campaign_budget.get("amountMicros", 0))
            }
            records.append(record)
        
        df = pd.DataFrame(records)
        logger.info(f"Extracted {len(df)} Google Ads records")
        return df
        
    except Exception as e:
        logger.error(f"Error extracting Google Ads data: {str(e)}")
        return pd.DataFrame()

# ------------------------
# LOAD INTO AZURE seleric_db
# ------------------------

def load_data_to_seleric_db(df, table_name=TABLE_NAME):
    """
    Load data to seleric_dbQL with proper connection management
    """
    try:
        # Use centralized database engine
        engine = get_db_engine()
        
        # Append new data
        df.to_sql(
            table_name,
            engine,
            if_exists='append',
            index=False
        )
        
        print("Loaded data into Azure seleric_db!")
        
    except Exception as e:
        print(f"Error loading data to seleric_dbQL: {e}")
        raise

# Only execute the data loading if this script is run directly
if __name__ == "__main__":
    load_data_to_seleric_db(df)
