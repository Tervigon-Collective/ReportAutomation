"""
Diagnostic: compare net profit from API vs DB for the last 7 days.
Run from the project root: python _compare_net_profit.py
"""
import os
import sys
import pandas as pd
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(__file__))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from api_data_fetcher import fetch_net_profit_single_day, fetch_net_profit_from_db

N_DAYS = 7
end_date   = date.today()
start_date = end_date - timedelta(days=N_DAYS - 1)
start_str  = start_date.strftime("%Y-%m-%d")
end_str    = end_date.strftime("%Y-%m-%d")

print(f"\n{'='*60}")
print(f"  Net Profit comparison  |  {start_str} → {end_str}")
print(f"{'='*60}\n")

# ── API ──────────────────────────────────────────────────────────
print("Fetching from API …")
api_resp = fetch_net_profit_single_day(start_date=start_str, end_date=end_str)
api_data  = api_resp.get("data", {})
api_days  = api_data.get("dailyBreakdowns", [])
api_totals = api_data.get("totals", {})

api_rows = []
for d in api_days:
    api_rows.append({
        "date":      d.get("date", ""),
        "revenue":   round(float(d.get("revenue",  0) or 0), 2),
        "cogs":      round(float(d.get("cogs",     0) or 0), 2),
        "ad_spend":  round(float((d.get("adSpend") or {}).get("total", 0)
                                 if isinstance(d.get("adSpend"), dict)
                                 else float(d.get("adSpend", 0) or 0)), 2),
        "net_profit": round(float(d.get("netProfit", 0) or 0), 2),
    })
api_df = pd.DataFrame(api_rows)

# ── DB ───────────────────────────────────────────────────────────
print("Fetching from DB …\n")
db_resp   = fetch_net_profit_from_db(n_days=N_DAYS)
db_df_raw = db_resp["dailyBreakdown"].copy()
db_totals = db_resp["totals"]

db_df = db_df_raw.rename(columns={
    "sale_date":    "date",
    "total_ad_spend": "ad_spend",
}).round(2)

# ── Daily comparison ─────────────────────────────────────────────
print("── Daily breakdown ──────────────────────────────────────")

if not api_df.empty:
    api_df["date"] = api_df["date"].astype(str).str[:10]

db_df["date"] = db_df["date"].astype(str).str[:10]

merged = pd.merge(
    api_df[["date","revenue","cogs","ad_spend","net_profit"]].rename(
        columns=lambda c: f"api_{c}" if c != "date" else c),
    db_df[["date","revenue","cogs","ad_spend","net_profit"]].rename(
        columns=lambda c: f"db_{c}" if c != "date" else c),
    on="date", how="outer"
).sort_values("date")

for col in ("revenue", "cogs", "ad_spend", "net_profit"):
    merged[f"diff_{col}"] = (merged[f"db_{col}"] - merged[f"api_{col}"]).round(2)

pd.set_option("display.max_columns", None)
pd.set_option("display.width", 160)
pd.set_option("display.float_format", "{:,.2f}".format)

print(merged[[
    "date",
    "api_revenue",  "db_revenue",  "diff_revenue",
    "api_cogs",     "db_cogs",     "diff_cogs",
    "api_ad_spend", "db_ad_spend", "diff_ad_spend",
    "api_net_profit","db_net_profit","diff_net_profit",
]].to_string(index=False))

# ── Totals comparison ─────────────────────────────────────────────
print("\n── Totals ───────────────────────────────────────────────")
rows = []
for metric, api_key, db_key in [
    ("Revenue",    "revenue",   "revenue"),
    ("COGS",       "cogs",      "cogs"),
    ("Ad Spend",   "adSpend",   "adSpend"),
    ("Net Profit", "netProfit", "netProfit"),
]:
    api_val = round(float(api_totals.get(api_key, 0) or 0), 2)
    db_val  = round(float(db_totals.get(db_key,  0) or 0), 2)
    diff    = round(db_val - api_val, 2)
    pct     = f"{diff/api_val*100:+.1f}%" if api_val else "n/a"
    rows.append({"Metric": metric, "API": api_val, "DB": db_val, "Diff (DB-API)": diff, "%": pct})

print(pd.DataFrame(rows).to_string(index=False))
print()
