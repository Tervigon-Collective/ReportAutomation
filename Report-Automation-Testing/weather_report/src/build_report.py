"""Phase 6: final campaign opportunity report + heatmap.

Takes the Phase 5 scored table and adds the decision layer:

  * rank                       by opportunity_score
  * opportunity_type           Scale / Test / Retarget / Prepare / Evergreen / Pause
  * recommended_action         a concrete instruction
  * reason                     why, referencing the weather + sales signals
  * recommended_product_group  monsoon product group for the weather bucket
  * recommended_products       example SKUs from monsoon_products.yaml

Writes:
  - reports/campaign_opportunity_{report_date}.csv
  - reports/campaign_opportunity_{report_date}_heatmap.png  (top-N sub-scores)
  - reports/campaign_opportunity_{report_date}_map.png      (geo opportunity)

Decision matrix (docs/weather_report.md): high weather + high sales -> Scale;
high weather + low/no sales + Tier1/2 -> Test; rain active + past customers ->
Retarget; emerging rain -> Prepare; low rain + high sales -> Evergreen;
low rain + low sales -> Pause. Thresholds in config/scoring_rules.json.

Usage (from weather_report/):
    py src/build_report.py
    py src/build_report.py --report-date 2026-06-26
    py src/build_report.py --top 25        # cities shown in the heatmap
    py src/build_report.py --no-plots
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("build_report")

MODULE_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = MODULE_ROOT / "data" / "weather"
SCORED_DIR = DATA_DIR / "scored"
REPORTS_DIR = DATA_DIR / "reports"
SCORING_RULES_PATH = DATA_DIR / "config" / "scoring_rules.json"
PRODUCTS_PATH = DATA_DIR / "config" / "monsoon_products.yaml"

IST = timezone(timedelta(hours=5, minutes=30))
CURRENT_DIR = DATA_DIR / "weather" / "current"


def _load_weather_fetched_at(report_date: str) -> str:
    """Return Open-Meteo fetch timestamp from the Phase 3 JSON payload, if present."""
    path = CURRENT_DIR / f"{report_date}.json"
    if not path.exists():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return str(payload.get("fetched_at") or "")
    except (OSError, json.JSONDecodeError):
        return ""


def _title_stamp(report_date: str, report_time: str) -> str:
    return f"{report_date} {report_time} IST"


# weather_bucket -> monsoon product group key
BUCKET_TO_GROUP = {
    "active_rain": "rain_protection",
    "heavy_rain_watch": "rain_protection",
    "high_rain_probability": "rain_protection",
    "emerging_rain": "rain_protection",
    "low_weather_opportunity": None,
    "moderate": None,
    "no_data": None,
}
GROUP_LABEL = {
    "rain_protection": "Rain Protection",
    "post_rain_care": "Post-Rain Care",
    "travel": "Travel",
    "indoor_comfort": "Indoor Comfort",
}

REPORT_COLUMNS = [
    "report_date", "report_time", "weather_fetched_at", "rank", "city_id", "city", "state", "region", "city_tier",
    "weather_status", "rain_now", "max_rain_probability_next_72h",
    "rainfall_next_3d_mm", "precipitation_hours_next_3d",
    "orders_7d", "orders_30d", "revenue_30d", "sales_momentum",
    "existing_sales_city", "new_opportunity_city",
    "weather_score", "sales_score", "market_size_score", "trend_score",
    "opportunity_score", "opportunity_type", "recommended_action",
    "recommended_product_group", "recommended_products", "reason", "created_at",
]


def _load_products() -> dict:
    try:
        import yaml
        data = yaml.safe_load(PRODUCTS_PATH.read_text(encoding="utf-8")) or {}
        return data.get("monsoon_products", {})
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load monsoon_products.yaml: %s", exc)
        return {}


def decide(row, rules: dict) -> tuple[str, str, str]:
    """Return (opportunity_type, recommended_action, reason)."""
    hi_w = rules.get("high_weather_score", 50)
    lo_w = rules.get("low_weather_score", 15)
    hi_sales = rules.get("high_sales_orders_30d", 30)
    retarget_min = rules.get("retarget_min_orders_90d", 10)
    test_tiers = set(rules.get("test_tiers", ["Tier 1", "Tier 2"]))

    wscore = float(row["weather_score"])
    bucket = str(row["weather_bucket"]) if "weather_bucket" in row else ""
    status = str(row["weather_status"])
    rain_now = bool(row["rain_now"])
    o30 = float(row["orders_30d"])
    o90 = float(row["orders_90d"]) if "orders_90d" in row else 0.0
    tier = str(row["city_tier"])
    prob = row["max_rain_probability_next_72h"]
    rain3d = row["rainfall_next_3d_mm"]

    high_weather = wscore >= hi_w or rain_now or status in (
        "Active Rain", "Heavy Rain Watch", "High Rain Probability")
    low_weather = status == "Low Weather Opportunity" or wscore < lo_w
    high_sales = o30 >= hi_sales
    established = o90 >= retarget_min   # a real customer base to re-engage
    new_market = o90 < retarget_min     # little / no sales history

    if low_weather:
        if high_sales:
            return ("Evergreen",
                    "Continue normal/evergreen campaign; avoid rain creative.",
                    f"Low rain signal but proven demand ({int(o30)} orders/30d) - keep selling, skip monsoon angle.")
        return ("Pause",
                "Do not allocate monsoon budget; deprioritize.",
                f"Low rain ({prob}% max 72h, {rain3d}mm/3d) and low sales ({int(o30)}/30d).")

    if high_weather and high_sales:
        return ("Scale",
                "Increase budget on rain/monsoon products.",
                f"Strong rain signal ({status.lower()}, weather score {wscore:.0f}) with proven sales ({int(o30)} orders/30d).")

    # New / no-sales market with a strong signal in a worthwhile tier -> Test.
    if high_weather and new_market and tier in test_tiers:
        return ("Test",
                "Launch a city test adset on monsoon products.",
                f"Strong rain signal ({status.lower()}) with little sales history ({int(o90)} orders/90d) in a {tier} market - test demand.")

    # Established base + active rain but soft recent sales -> Retarget.
    if rain_now and established and not high_sales:
        return ("Retarget",
                "Retarget past buyers with monsoon products.",
                f"Active rain now with an existing customer base ({int(o90)} orders/90d) but soft recent sales ({int(o30)}/30d).")

    if bucket == "emerging_rain" or status == "Emerging Rain":
        return ("Prepare",
                "Prepare creatives and budget ahead of rain.",
                f"Rain emerging in 24-72h ({prob}% probability) - get monsoon assets ready.")

    if high_weather and not high_sales:
        return ("Test",
                "Test a small monsoon budget.",
                f"Rain signal present ({status.lower()}) but small market ({tier}) and low sales - start small.")

    # moderate weather fallback
    if high_sales:
        return ("Evergreen",
                "Continue normal campaign.",
                f"Moderate weather with steady demand ({int(o30)} orders/30d).")
    return ("Pause",
            "Monitor; no monsoon push yet.",
            f"Moderate weather ({prob}% max 72h) and limited sales ({int(o30)}/30d).")


def build(report_date: str, products: dict, rules: dict) -> pd.DataFrame:
    scored_path = SCORED_DIR / f"{report_date}.csv"
    if not scored_path.exists():
        raise SystemExit(f"No scored file for {report_date}: {scored_path}\n"
                         f"Run score_opportunity.py first.")
    df = pd.read_csv(scored_path)
    df = df.sort_values("opportunity_score", ascending=False).reset_index(drop=True)
    df["rank"] = df.index + 1

    decisions = df.apply(lambda r: decide(r, rules), axis=1, result_type="expand")
    df["opportunity_type"] = decisions[0]
    df["recommended_action"] = decisions[1]
    df["reason"] = decisions[2]

    def group_for(row):
        # Pause/Evergreen never push monsoon products.
        if row["opportunity_type"] in ("Pause", "Evergreen"):
            return None
        return BUCKET_TO_GROUP.get(str(row["weather_bucket"]))

    grp_keys = df.apply(group_for, axis=1)
    df["recommended_product_group"] = grp_keys.map(lambda g: GROUP_LABEL.get(g, "") if g else "")
    df["recommended_products"] = grp_keys.map(
        lambda g: ", ".join(products.get(g, [])) if g else "")

    now = datetime.now(IST)
    df["report_time"] = now.strftime("%H:%M")
    df["weather_fetched_at"] = _load_weather_fetched_at(report_date)
    df["created_at"] = now.isoformat(timespec="seconds")
    return df[REPORT_COLUMNS]


# ----------------------------- plotting --------------------------------------
def _plot_heatmap(report: pd.DataFrame, top: int, out_path: Path,
                  report_date: str, report_time: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    cols = ["weather_score", "sales_score", "market_size_score",
            "trend_score", "opportunity_score"]
    col_labels = ["Weather", "Sales", "Market", "Trend", "Opportunity"]
    d = report.head(top)
    mat = d[cols].to_numpy(dtype=float)
    ylabels = [f"{r['city']}  ·  {r['weather_status']}" for _, r in d.iterrows()]

    fig, ax = plt.subplots(figsize=(9, max(5, 0.42 * len(d) + 1.5)))
    im = ax.imshow(mat, cmap="RdYlGn", aspect="auto", vmin=0, vmax=100)
    ax.set_xticks(range(len(col_labels)), labels=col_labels)
    ax.set_yticks(range(len(d)), labels=ylabels, fontsize=8)
    ax.set_title(
        f"Weather Campaign Opportunity - top {len(d)} cities ({_title_stamp(report_date, report_time)})",
        fontsize=12, pad=12,
    )
    for i in range(mat.shape[0]):
        for j in range(mat.shape[1]):
            v = mat[i, j]
            ax.text(j, i, f"{v:.0f}", ha="center", va="center", fontsize=7,
                    color="black" if 25 <= v <= 80 else "white")
    cbar = fig.colorbar(im, ax=ax, fraction=0.025, pad=0.02)
    cbar.set_label("Score (0-100)")
    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    plt.close(fig)


def _plot_geo(report: pd.DataFrame, master_path: Path, out_path: Path,
              report_date: str, report_time: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    master = pd.read_csv(master_path)[["city_id", "latitude", "longitude"]]
    d = report.merge(master, on="city_id", how="left").dropna(subset=["latitude", "longitude"])
    sizes = 30 + (d["orders_30d"].clip(lower=0) ** 0.5) * 12

    fig, ax = plt.subplots(figsize=(8, 9))
    sc = ax.scatter(d["longitude"], d["latitude"], c=d["weather_score"],
                    s=sizes, cmap="RdYlGn", vmin=0, vmax=100,
                    edgecolors="black", linewidths=0.4, alpha=0.9)
    for _, r in d.head(15).iterrows():
        ax.annotate(r["city"], (r["longitude"], r["latitude"]), fontsize=7,
                    xytext=(3, 3), textcoords="offset points")
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_title(
        f"Weather geo-heatmap ({_title_stamp(report_date, report_time)})\n"
        f"colour = weather_score, size = orders_30d",
        fontsize=11,
    )
    cbar = fig.colorbar(sc, ax=ax, fraction=0.035, pad=0.02)
    cbar.set_label("weather_score")
    ax.grid(True, alpha=0.2)
    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    plt.close(fig)


def _plot_combined_canvas(
    report: pd.DataFrame,
    master_path: Path,
    top: int,
    out_path: Path,
    report_date: str,
    report_time: str,
) -> None:
    """Single PNG with score heatmap (top) and geo scatter (bottom) for email inline display."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    cols = ["weather_score", "sales_score", "market_size_score",
            "trend_score", "opportunity_score"]
    col_labels = ["Weather", "Sales", "Market", "Trend", "Opportunity"]
    heat = report.head(top)
    mat = heat[cols].to_numpy(dtype=float)
    ylabels = [f"{r['city']}  ·  {r['weather_status']}" for _, r in heat.iterrows()]

    master = pd.read_csv(master_path)[["city_id", "latitude", "longitude"]]
    geo = report.merge(master, on="city_id", how="left").dropna(subset=["latitude", "longitude"])
    sizes = 30 + (geo["orders_30d"].clip(lower=0) ** 0.5) * 12

    fig = plt.figure(figsize=(10, max(10, 0.35 * len(heat) + 7)))
    gs = fig.add_gridspec(2, 1, height_ratios=[max(1.2, 0.04 * len(heat) + 0.8), 1.1], hspace=0.35)

    ax_hm = fig.add_subplot(gs[0])
    im = ax_hm.imshow(mat, cmap="RdYlGn", aspect="auto", vmin=0, vmax=100)
    ax_hm.set_xticks(range(len(col_labels)), labels=col_labels)
    ax_hm.set_yticks(range(len(heat)), labels=ylabels, fontsize=8)
    ax_hm.set_title(
        f"Top {len(heat)} cities — sub-scores ({_title_stamp(report_date, report_time)})",
        fontsize=11, pad=8,
    )
    for i in range(mat.shape[0]):
        for j in range(mat.shape[1]):
            v = mat[i, j]
            ax_hm.text(j, i, f"{v:.0f}", ha="center", va="center", fontsize=7,
                       color="black" if 25 <= v <= 80 else "white")

    ax_geo = fig.add_subplot(gs[1])
    sc = ax_geo.scatter(geo["longitude"], geo["latitude"], c=geo["weather_score"],
                        s=sizes, cmap="RdYlGn", vmin=0, vmax=100,
                        edgecolors="black", linewidths=0.4, alpha=0.9)
    for _, r in geo.head(12).iterrows():
        ax_geo.annotate(r["city"], (r["longitude"], r["latitude"]), fontsize=7,
                        xytext=(3, 3), textcoords="offset points")
    ax_geo.set_xlabel("Longitude")
    ax_geo.set_ylabel("Latitude")
    ax_geo.set_title("Weather map — colour = weather_score, size = orders (30d)", fontsize=11)
    ax_geo.grid(True, alpha=0.2)

    fig.colorbar(im, ax=ax_hm, fraction=0.02, pad=0.02, label="Score (0-100)")
    fig.colorbar(sc, ax=ax_geo, fraction=0.02, pad=0.02, label="weather_score")
    fig.suptitle(
        f"Weather Campaign Opportunity — {_title_stamp(report_date, report_time)}",
        fontsize=13, y=0.98,
    )
    fig.subplots_adjust(top=0.93, hspace=0.45)
    fig.savefig(out_path, dpi=130, bbox_inches="tight")
    plt.close(fig)


def run(
    report_date: str,
    top: int,
    plots: bool,
    combined_path: Path | None = None,
    csv_path: Path | None = None,
) -> dict:
    products = _load_products()
    rules = json.loads(SCORING_RULES_PATH.read_text(encoding="utf-8")).get("action_rules", {})

    report = build(report_date, products, rules)
    report_time = str(report["report_time"].iloc[0]) if not report.empty else datetime.now(IST).strftime("%H:%M")
    weather_fetched_at = str(report["weather_fetched_at"].iloc[0]) if not report.empty else _load_weather_fetched_at(report_date)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_csv = csv_path or (REPORTS_DIR / f"campaign_opportunity_{report_date}.csv")
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    report.to_csv(out_csv, index=False)
    logger.info("Wrote %d rows -> %s", len(report), out_csv)

    canonical = REPORTS_DIR / f"campaign_opportunity_{report_date}.csv"
    if out_csv != canonical:
        try:
            report.to_csv(canonical, index=False)
            logger.info("Wrote canonical copy -> %s", canonical)
        except OSError as exc:
            logger.warning("Canonical report copy skipped (%s): %s", canonical, exc)

    logger.info("Action mix: %s", report["opportunity_type"].value_counts().to_dict())
    logger.info("Top 10:\n%s", report.head(10)[
        ["rank", "city", "city_tier", "weather_status", "opportunity_score",
         "opportunity_type", "recommended_product_group"]].to_string(index=False))

    out: dict = {
        "csv_path": out_csv,
        "report_date": report_date,
        "report_time": report_time,
        "weather_fetched_at": weather_fetched_at,
        "heatmap": None,
        "map": None,
        "combined": None,
    }

    if plots:
        hm = REPORTS_DIR / f"campaign_opportunity_{report_date}_heatmap.png"
        gm = REPORTS_DIR / f"campaign_opportunity_{report_date}_map.png"
        _plot_heatmap(report, top, hm, report_date, report_time)
        out["heatmap"] = hm
        logger.info("Wrote heatmap -> %s", hm)
        try:
            _plot_geo(report, DATA_DIR / "city_master.csv", gm, report_date, report_time)
            out["map"] = gm
            logger.info("Wrote geo-heatmap -> %s", gm)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Geo plot skipped: %s", exc)

    if combined_path is not None:
        try:
            _plot_combined_canvas(report, DATA_DIR / "city_master.csv", top,
                                  combined_path, report_date, report_time)
            out["combined"] = combined_path
            logger.info("Wrote combined canvas -> %s", combined_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Combined canvas skipped: %s", exc)

    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--report-date", default=None, help="Report date YYYY-MM-DD (default today IST).")
    p.add_argument("--top", type=int, default=25, help="Cities shown in the heatmap (default 25).")
    p.add_argument("--no-plots", action="store_true", help="Skip heatmap/geo PNGs.")
    args = p.parse_args()
    report_date = args.report_date or datetime.now(IST).strftime("%Y-%m-%d")
    run(report_date, args.top, not args.no_plots)


if __name__ == "__main__":
    main()
