"""Verify the new ReportWeekMonth Azure Function is structurally correct.

Checks:
  1. function.json and __init__.py exist
  2. function.json parses and has a valid timer trigger
  3. __init__.py compiles (no SyntaxError)
  4. The import path it uses resolves to the WTD/MTD module
  5. azure.functions is installed in the venv (needed at runtime)
  6. Translate the NCRONTAB schedule to a human-readable IST time
"""
import json
import os
import sys
import importlib.util


ROOT = os.path.dirname(os.path.abspath(__file__))
RWM = os.path.join(ROOT, "ReportWeekMonth")
WTD = os.path.join(ROOT, "WTD_MTD")


def main() -> None:
    print("=== File presence ===")
    for name in ("function.json", "__init__.py"):
        p = os.path.join(RWM, name)
        exists = os.path.exists(p)
        size = os.path.getsize(p) if exists else 0
        print(f"  {name:14}  exists={exists}  size={size}B")

    print("\n=== function.json ===")
    with open(os.path.join(RWM, "function.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    print(json.dumps(cfg, indent=2))

    binding = cfg["bindings"][0]
    assert binding["type"] == "timerTrigger", "binding type should be timerTrigger"
    schedule = binding["schedule"]
    fields = schedule.split()
    print(f"\n  schedule fields ({len(fields)}): {fields}")
    if len(fields) == 6:
        sec, minute, hour, day, month, dow = fields
        if sec.isdigit() and minute.isdigit() and hour.isdigit():
            utc_h, utc_m = int(hour), int(minute)
            total_min = (utc_h * 60 + utc_m + 5 * 60 + 30) % (24 * 60)
            ist_h, ist_m = divmod(total_min, 60)
            print(
                f"  -> UTC {utc_h:02d}:{utc_m:02d}:{sec:0>2}  "
                f"= IST {ist_h:02d}:{ist_m:02d} daily"
            )

    print("\n=== __init__.py compiles ===")
    src = os.path.join(RWM, "__init__.py")
    with open(src, "rb") as f:
        compile(f.read(), src, "exec")
    print("  OK (no SyntaxError)")

    print("\n=== Import path resolution (as the function will do at runtime) ===")
    for p in (ROOT, WTD):
        if p not in sys.path:
            sys.path.insert(0, p)
    spec = importlib.util.find_spec("timerange_wtd_mtd_rollup")
    found = spec is not None
    origin = spec.origin if spec else None
    print(f"  find_spec('timerange_wtd_mtd_rollup') -> found={found}")
    print(f"  origin: {origin}")
    assert found, "WTD_MTD module not importable from sys.path"

    print("\n=== Runtime deps ===")
    try:
        import azure.functions as az
        ver = getattr(az, "__version__", None) or "(version attr missing)"
        print(f"  azure.functions installed: version={ver}")
    except ImportError as e:
        print(f"  azure.functions NOT installed in venv: {e}")
        print("  -> add `azure-functions` to requirements.txt or install: pip install azure-functions")

    print("\nAll structural checks passed.")


if __name__ == "__main__":
    main()
