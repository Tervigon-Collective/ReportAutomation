"""Light-weight check: print the WTD/MTD/Daily windows used by each channel,
without hitting any APIs.

- Meta / Google / Organic: rolling 7-day / 30-day windows ending today.
- Amazon (sheets + email summary): calendar Mon->today / 1st->today,
  then end shifted back 1 day for the gold-table lag.
"""
import sys, os
from datetime import timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "WTD_MTD"))
from timerange_wtd_mtd_rollup import (  # noqa: E402
    get_wtd_mtd_timeframes,
    get_amazon_calendar_timeframes,
)


def fmt(d):
    return d.strftime("%Y-%m-%d (%a %d-%m)")


def width(start, end):
    return (end.date() - start.date()).days + 1


tf = get_wtd_mtd_timeframes()
amz = get_amazon_calendar_timeframes()

print("=== Rolling windows (Meta / Google / Organic) ===\n")
for key in ("wtd", "mtd", "daily"):
    s = tf[key]["start_date"]
    e = tf[key]["end_date"]
    label = tf[key]["label"]
    print(f"  {label} ({key}):")
    print(f"    start : {fmt(s)}")
    print(f"    end   : {fmt(e)}")
    print(f"    days  : {width(s, e)}")
    sheet_lbl = f"{s.strftime('%d-%m')} to {e.strftime('%d-%m')}"
    print(f"    sheet : ({sheet_lbl})\n")

print("=== Calendar windows (Amazon sheets - no lag) ===\n")
for key in ("wtd", "mtd"):
    s = amz[key]["start_date"]
    e = amz[key]["end_date"]
    label = amz[key]["label"]
    sheet_lbl = f"{s.strftime('%d-%m')} to {e.strftime('%d-%m')}"
    print(f"  {label} ({key}):")
    print(f"    start : {fmt(s)}")
    print(f"    end   : {fmt(e)}")
    print(f"    days  : {width(s, e)}")
    print(f"    sheet : ({sheet_lbl})\n")

print("=== Amazon ranges WITH -1 day gold lag (sheet + email) ===\n")
for key in ("wtd", "mtd"):
    s = amz[key]["start_date"]
    e = amz[key]["end_date"] - timedelta(days=1)
    sheet_lbl = f"{s.strftime('%d-%m')} to {e.strftime('%d-%m')}"
    print(f"  Amazon {key.upper()}: {fmt(s)}  ->  {fmt(e)}   ({sheet_lbl})  [{width(s, e)} days]")
