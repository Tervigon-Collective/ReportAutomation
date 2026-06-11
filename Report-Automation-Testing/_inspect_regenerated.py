"""Print contents of the regenerated wtd_amazon Excel for verification."""
import pandas as pd
from tabulate import tabulate

XLSX = "wtd_amazon_regenerated.xlsx"
xls = pd.ExcelFile(XLSX)
print(f"\nSheets in {XLSX}: {xls.sheet_names}\n")
for name in xls.sheet_names:
    df = pd.read_excel(xls, sheet_name=name)
    print(f"--- {name} ({len(df)} rows) ---")
    print(tabulate(df, headers="keys", tablefmt="simple", showindex=False, floatfmt=".2f"))
    print()
