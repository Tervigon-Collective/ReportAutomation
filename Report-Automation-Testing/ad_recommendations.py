import pandas as pd
import logging
from database_manager import get_db_engine, dispose_engine
import os
from datetime import datetime
from global_config import get_temp_dir

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def fetch_ad_recommendations():
    """
    Fetch the latest batch of ad recommendations from the database (all rows with the most recent created_at timestamp).
    Returns a DataFrame with all columns from ad_recommendations table.
    """
    try:
        engine = get_db_engine()
        # Step 1: Get the latest created_at timestamp
        latest_ts_query = "SELECT MAX(created_at) as latest_ts FROM ad_recommendations"
        latest_ts_df = pd.read_sql(latest_ts_query, engine)
        latest_ts = latest_ts_df.iloc[0]['latest_ts']
        if pd.isnull(latest_ts):
            logger.warning("No ad recommendations found in database")
            return pd.DataFrame()
        # Step 2: Fetch all rows with that timestamp
        query = "SELECT * FROM ad_recommendations WHERE created_at = %s"
        logger.info(f"Fetching ad recommendations from database for created_at = {latest_ts}...")
        df = pd.read_sql(query, engine, params=(latest_ts,))
        logger.info(f"Successfully fetched {len(df)} ad recommendations for latest timestamp")
        return df
    except Exception as e:
        logger.error(f"Error fetching ad recommendations: {str(e)}")
        raise
    finally:
        dispose_engine()

def write_merged_excel(df, output_path):
    """
    Write DataFrame to Excel with merged cells for campaign and adset columns.
    Removes: campaign_id, adset_id, ad_id, rule_type, actions, error
    Sets column widths and enables word wrap for readability, except for 'signal_monitored'.
    Increases 'insight' column width for better readability.
    """
    # Remove specified columns if present
    drop_cols = ['campaign_id', 'adset_id', 'ad_id', 'rule_type', 'actions', 'error', 'id']
    df = df.drop(columns=[col for col in drop_cols if col in df.columns], errors='ignore')
    df = df.sort_values(['campaign_name', 'adset_name', 'ad_name'])

    # Reorder columns as requested
    desired_order = ['campaign_name', 'adset_name', 'ad_name', 'insight', 'signal_monitored', 'rule_names',  'created_at']
    remaining_cols = [col for col in df.columns if col not in desired_order]
    ordered_cols = [col for col in desired_order if col in df.columns] + remaining_cols
    df = df[ordered_cols]

    # Convert created_at from UTC to IST (Asia/Kolkata)
    if 'created_at' in df.columns:
        df['created_at'] = pd.to_datetime(df['created_at'], errors='coerce')
        if df['created_at'].dt.tz is None or str(df['created_at'].dt.tz) == 'None':
            df['created_at'] = df['created_at'].dt.tz_localize('UTC')
        df['created_at'] = df['created_at'].dt.tz_convert('Asia/Kolkata')
        df['created_at'] = df['created_at'].dt.strftime('%Y-%m-%d %H:%M:%S')
    
    with pd.ExcelWriter(output_path, engine='xlsxwriter') as writer:
        df.to_excel(writer, index=False, sheet_name='Sheet1')
        workbook = writer.book
        worksheet = writer.sheets['Sheet1']
        
        # Merge cells for campaign_name and adset_name columns
        merge_cols = ['campaign_name', 'adset_name']
        col_indices = [df.columns.get_loc(col) for col in merge_cols if col in df.columns]
        
        for col_idx in col_indices:
            start_row = 1  # skip header
            last_value = None
            for row in range(1, len(df) + 1):
                value = df.iloc[row - 1, col_idx]
                if value != last_value and last_value is not None:
                    if row - start_row > 1:
                        worksheet.merge_range(start_row, col_idx, row - 1, col_idx, last_value)
                    start_row = row
                last_value = value
            # Merge the last group
            if len(df) + 1 - start_row > 1:
                worksheet.merge_range(start_row, col_idx, len(df), col_idx, last_value)
        
        # Set column widths and enable word wrap, except for 'signal_monitored'
        wrap_format = workbook.add_format({'text_wrap': True, 'valign': 'top'})
        for i, col in enumerate(df.columns):
            max_len = max(df[col].astype(str).map(len).max(), len(col))
            # Increase width for 'insight' column
            if col == 'insight':
                width = 60  # wider for insights
                worksheet.set_column(i, i, width, wrap_format)
            elif col == 'signal_monitored':
                width = min(max_len + 4, 40)
                worksheet.set_column(i, i, width)  # no wrap
            else:
                width = min(max_len + 4, 40)
                worksheet.set_column(i, i, width, wrap_format)

def generate_ad_recommendations_report(output_dir=None):
    """
    Generate ad recommendations Excel report.
    
    Args:
        output_dir (str): Directory to save the report. If None, uses temp directory.
    """
    try:
        # Use temp directory if not specified
        if output_dir is None:
            output_dir = get_temp_dir()
        # Create output directory if it doesn't exist
        os.makedirs(output_dir, exist_ok=True)
        
        # Fetch data from database
        df = fetch_ad_recommendations()
        
        if df.empty:
            logger.warning("No ad recommendations found in database")
            return None
        
        # Generate output filename with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_filename = f"actionable_insight_{timestamp}.xlsx"
        output_path = os.path.join(output_dir, output_filename)
        
        # Write Excel file with merged cells
        logger.info(f"Generating Excel report: {output_path}")
        write_merged_excel(df, output_path)
        
        logger.info(f"Ad recommendations report generated successfully: {output_path}")
        return output_path
        
    except Exception as e:
        logger.error(f"Error generating ad recommendations report: {str(e)}")
        raise

def main():
    """
    Main function to generate ad recommendations report.
    """
    try:
        output_path = generate_ad_recommendations_report()
        if output_path:
            print(f"✅ Ad recommendations report generated successfully: {output_path}")
        else:
            print("⚠️ No ad recommendations data found to generate report")
            
    except Exception as e:
        logger.error(f"Failed to generate ad recommendations report: {str(e)}")
        print(f"❌ Error generating report: {str(e)}")

if __name__ == "__main__":
    main()
