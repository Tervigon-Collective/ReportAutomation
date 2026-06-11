"""Sanity-check the extended Amazon summary fields for the WTD range.

Prints the dict returned by get_amazon_clickhouse_summary plus a preview of
the values that will fill the Channel Performance Amazon row.
"""
from datetime import datetime

from amazon_entity_report import get_amazon_clickhouse_summary


def main() -> None:
    # WTD range matching the email screenshot: 02-06 to 07-06 (gold lag 1 day).
    summary = get_amazon_clickhouse_summary(
        datetime(2026, 6, 2), datetime(2026, 6, 7)
    )

    print("\n--- get_amazon_clickhouse_summary (WTD 02-06..07-06) ---")
    for k, v in summary.items():
        if isinstance(v, float):
            print(f"  {k:>26}: {v:>14,.4f}")
        else:
            print(f"  {k:>26}: {v}")

    print("\n--- Channel-table row preview ---")
    rev = summary["revenue"]
    cogs = summary["cogs"]
    spend = summary["spend"]
    profit = summary["net_profit"]
    roas = summary["net_roas"]
    units = summary["units"]
    orders = summary["orders"]
    pnl = summary["pnl_available"]
    # Use INR prefix instead of the rupee symbol to avoid Windows cp1252 issues
    # when this script is run in a non-UTF-8 console. The actual HTML email
    # output uses the proper symbol.
    print(
        f"  Amazon* | Revenue INR {rev:,.2f} | "
        f"COGS {'INR {:,.2f}'.format(cogs) if pnl else 'N/A'} | "
        f"Spend INR {spend:,.2f} | "
        f"Net Profit {'INR {:,.2f}'.format(profit) if pnl else 'N/A'} | "
        f"Net ROAS {('{:.2f}'.format(roas) if spend > 0 else 'N/A') if pnl else 'N/A'} | "
        f"Orders {orders:,} | "
        f"Units {'{:,}'.format(units) if pnl else 'N/A'}"
    )


if __name__ == "__main__":
    main()
