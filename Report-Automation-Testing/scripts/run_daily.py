#!/usr/bin/env python3
import sys
import os
import logging

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WTD_MTD_DIR = os.path.join(PROJECT_ROOT, "WTD_MTD")
for _p in (PROJECT_ROOT, WTD_MTD_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from timerange_wtd_mtd_rollup import main

if __name__ == "__main__":
    main()
