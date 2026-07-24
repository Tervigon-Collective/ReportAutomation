"""One-shot reconciliation runner: report layer vs Seleric dashboard, all windows.

Runs every reconciliation gate and prints a PASS/FAIL matrix. Use this as the single
command to prove the email/PDF reports tie to the dashboard on current data.

Gates (each is an independent script; exit 0 == pass):
  metric layer        _verify_dashboard_alignment.py   DAILY/WTD/MTD totals + channels + ROAS
  channel table       _verify_channel_recon.py         channel table + residual == Total (2 paths)
  daily PDF path      _verify_daily_pdf.py             daily PDF channels == dashboard
  WTD/MTD email       _verify_wtd_mtd_email.py         email body totals/channels/Amazon + residual
  entity Excel        _verify_entity_recon.py          entity rollups internal + ad-spend vs canonical
  Amazon vs dash      _verify_amazon_recon.py          settlement-vs-dashboard Amazon divergence (diag)
  producer replica    _verify_producer_gold.py         gold.fct_daily_pnl consistency + freshness (diag)

No emails are sent by any gate.
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

GATES = [
    ("metric layer  (DAILY/WTD/MTD totals+channels+ROAS)", "_verify_dashboard_alignment.py", True),
    ("channel table (rows+residual == Total)", "_verify_channel_recon.py", True),
    ("daily PDF path (channels == dashboard)", "_verify_daily_pdf.py", True),
    ("WTD/MTD email (body totals/channels/Amazon+residual)", "_verify_wtd_mtd_email.py", True),
    ("entity Excel  (rollups + ad-spend vs canonical)", "_verify_entity_recon.py", True),
    ("Amazon vs dashboard (settlement divergence diag)", "_verify_amazon_recon.py", False),
    ("producer replica (gold consistency + freshness diag)", "_verify_producer_gold.py", False),
]


def run(script: str) -> tuple[int, str]:
    p = subprocess.run([PY, os.path.join(HERE, script)], capture_output=True, text=True)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def main() -> int:
    results = []
    all_gating_ok = True
    for label, script, gating in GATES:
        print(f"\n{'='*78}\n RUN  {script}\n{'='*78}")
        code, out = run(script)
        # print a compact tail so the full evidence stays visible but not overwhelming
        tail = [ln for ln in out.splitlines() if ln.strip()][-14:]
        print("\n".join(tail))
        ok = (code == 0)
        results.append((label, script, ok, gating))
        if gating and not ok:
            all_gating_ok = False

    print(f"\n{'#'*78}\n RECONCILIATION MATRIX\n{'#'*78}")
    for label, script, ok, gating in results:
        kind = "GATE" if gating else "DIAG"
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] ({kind}) {label}")
    print(f"\n{'ALL GATES PASSED — reports reconcile to the dashboard' if all_gating_ok else 'SOME GATES FAILED'}")
    return 0 if all_gating_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
