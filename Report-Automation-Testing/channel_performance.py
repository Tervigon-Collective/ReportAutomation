"""
Channel performance metrics from ClickHouse gold tables.
Used for daily / WTD / MTD report bar charts (revenue, ad spend, orders by platform).
"""
from __future__ import annotations

import os
import logging
from datetime import date, datetime, timedelta
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

try:
    from amazon_entity_report import get_clickhouse_client
except ImportError:
    get_clickhouse_client = None

PLATFORM_ORDER = ["meta", "google", "organic", "other"]
PLATFORM_LABELS = {
    "meta": "Meta",
    "google": "Google",
    "organic": "Organic",
    "other": "Other",
}
PLATFORM_COLORS = {
    "meta": "#1877F2",
    "google": "#E53935",
    "organic": "#2EAA63",
    "other": "#78909C",
}
PLATFORM_MARKERS = {
    "meta": "o",
    "google": "s",
    "organic": "^",
    "other": "D",
}

METRIC_COLORS = {
    "revenue": "#1A7F4E",
    "ad_spend": "#E07B00",
    "orders": "#4A56E2",
}
METRIC_LABELS = {
    "revenue": "Gross Revenue",
    "ad_spend": "Ad Spend",
    "orders": "Orders",
}

_CURRENCY_SYMBOL: Optional[str] = None
_RUPEE_CHAR = "\u20b9"


def _currency_symbol() -> str:
    """Return ₹ when the active chart font supports it, otherwise Rs."""
    global _CURRENCY_SYMBOL
    if _CURRENCY_SYMBOL is not None:
        return _CURRENCY_SYMBOL

    if os.getenv("CURRENCY_USE_RS", "").lower() in ("1", "true", "yes"):
        _CURRENCY_SYMBOL = "Rs"
        return _CURRENCY_SYMBOL

    families = matplotlib.rcParams.get("font.sans-serif", ["DejaVu Sans"])
    if isinstance(families, str):
        families = [families]
    # DejaVu Sans (default Agg backend font) does not reliably render ₹ in PNG output.
    if any("dejavu" in str(f).lower() for f in families):
        _CURRENCY_SYMBOL = "Rs"
        return _CURRENCY_SYMBOL

    try:
        from matplotlib import font_manager, ft2font

        for name in families:
            path = font_manager.findfont(font_manager.FontProperties(family=name))
            font = ft2font.FT2Font(path)
            if font.get_char_index(ord(_RUPEE_CHAR)) != 0:
                _CURRENCY_SYMBOL = _RUPEE_CHAR
                return _CURRENCY_SYMBOL
    except Exception:
        pass

    _CURRENCY_SYMBOL = "Rs"
    return _CURRENCY_SYMBOL


def _to_date_str(value: str | date | datetime) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]


def get_brand_id() -> int:
    raw = os.getenv("CLICKHOUSE_BRAND_ID")
    if raw:
        return int(raw)
    try:
        from global_config import get_global_config

        return int(get_global_config("CLICKHOUSE_BRAND_ID", "20"))
    except (ImportError, ValueError, TypeError):
        return 20


def fetch_channel_performance(
    start_date: str | date | datetime,
    end_date: str | date | datetime,
    brand_id: Optional[int] = None,
) -> pd.DataFrame:
    """Fetch daily channel performance rows from ClickHouse gold."""
    if get_clickhouse_client is None:
        raise ImportError(
            "clickhouse-connect is required. Install with: pip install clickhouse-connect"
        )

    start_str = _to_date_str(start_date)
    end_str = _to_date_str(end_date)
    if brand_id is None:
        brand_id = get_brand_id()

    query = """
        SELECT
            toString(d.report_date) AS report_date,
            ch.platform AS platform,
            coalesce(c.attributed_orders, 0) AS attributed_orders,
            round(coalesce(c.gross_revenue_excl_gst, 0), 2) AS gross_revenue_excl_gst,
            coalesce(s.ad_spend, 0) AS ad_spend,
            round(
                if(coalesce(s.ad_spend, 0) > 0,
                   coalesce(c.gross_revenue_excl_gst, 0) / s.ad_spend,
                   NULL),
                2
            ) AS gross_roas
        FROM (
            SELECT DISTINCT report_date
            FROM gold.fct_daily_pnl
            WHERE brand_id = %(brand_id)s
              AND report_date >= toDate(%(start_date)s)
              AND report_date <= toDate(%(end_date)s)
        ) AS d
        CROSS JOIN (
            SELECT arrayJoin(['meta', 'google', 'organic', 'other']) AS platform
        ) AS ch
        LEFT JOIN (
            SELECT
                order_date AS report_date,
                coalesce(nullIf(lt_platform, ''), 'other') AS platform,
                toInt64(count()) AS attributed_orders,
                toFloat64(sum(gross_revenue)) / 1.18 AS gross_revenue_excl_gst
            FROM gold.fct_order_attribution
            WHERE brand_id = %(brand_id)s
              AND order_date >= toDate(%(start_date)s)
              AND order_date <= toDate(%(end_date)s)
            GROUP BY report_date, platform
        ) AS c
            ON d.report_date = c.report_date AND ch.platform = c.platform
        LEFT JOIN (
            SELECT 'meta' AS platform, report_date, toFloat64(meta_spend) AS ad_spend
            FROM gold.fct_daily_pnl
            WHERE brand_id = %(brand_id)s
              AND report_date >= toDate(%(start_date)s)
              AND report_date <= toDate(%(end_date)s)
            UNION ALL
            SELECT 'google', report_date, toFloat64(google_spend)
            FROM gold.fct_daily_pnl
            WHERE brand_id = %(brand_id)s
              AND report_date >= toDate(%(start_date)s)
              AND report_date <= toDate(%(end_date)s)
            UNION ALL
            SELECT 'organic', report_date, toFloat64(0)
            FROM gold.fct_daily_pnl
            WHERE brand_id = %(brand_id)s
              AND report_date >= toDate(%(start_date)s)
              AND report_date <= toDate(%(end_date)s)
            UNION ALL
            SELECT 'other', report_date, toFloat64(0)
            FROM gold.fct_daily_pnl
            WHERE brand_id = %(brand_id)s
              AND report_date >= toDate(%(start_date)s)
              AND report_date <= toDate(%(end_date)s)
        ) AS s
            ON d.report_date = s.report_date AND ch.platform = s.platform
        ORDER BY report_date, ch.platform
    """
    client = get_clickhouse_client()
    params = {
        "brand_id": int(brand_id),
        "start_date": start_str,
        "end_date": end_str,
    }
    result = client.query(query, parameters=params)
    df = pd.DataFrame(result.result_rows, columns=result.column_names)
    if df.empty:
        return df

    for col in ("attributed_orders", "gross_revenue_excl_gst", "ad_spend"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    if "gross_roas" in df.columns:
        df["gross_roas"] = pd.to_numeric(df["gross_roas"], errors="coerce")
    if "attributed_orders" in df.columns:
        df["attributed_orders"] = df["attributed_orders"].astype(int)
    return df


def aggregate_channel_performance(df: pd.DataFrame) -> pd.DataFrame:
    """Sum daily rows into one row per platform."""
    if df.empty:
        return df
    agg = (
        df.groupby("platform", as_index=False)
        .agg(
            attributed_orders=("attributed_orders", "sum"),
            gross_revenue_excl_gst=("gross_revenue_excl_gst", "sum"),
            ad_spend=("ad_spend", "sum"),
        )
    )
    agg["gross_roas"] = np.where(
        agg["ad_spend"] > 0,
        agg["gross_revenue_excl_gst"] / agg["ad_spend"],
        np.nan,
    )
    agg["gross_revenue_excl_gst"] = agg["gross_revenue_excl_gst"].round(2)
    agg["ad_spend"] = agg["ad_spend"].round(2)
    agg["gross_roas"] = agg["gross_roas"].round(2)
    order_map = {p: i for i, p in enumerate(PLATFORM_ORDER)}
    agg["_sort"] = agg["platform"].map(order_map).fillna(99)
    return agg.sort_values("_sort").drop(columns=["_sort"]).reset_index(drop=True)


def _format_inr(value: float) -> str:
    sym = _currency_symbol()
    if abs(value) >= 100_000:
        return f"{sym}{value / 100_000:.1f}L"
    if abs(value) >= 1_000:
        return f"{sym}{value / 1_000:.1f}K"
    return f"{sym}{value:,.0f}"


def _adjust_color(hex_color: str, *, lighten: float = 0.0, darken: float = 0.0) -> str:
    import matplotlib.colors as mcolors

    rgb = np.array(mcolors.to_rgb(hex_color))
    if lighten > 0:
        rgb = rgb + (1.0 - rgb) * lighten
    if darken > 0:
        rgb = rgb * (1.0 - darken)
    return mcolors.to_hex(np.clip(rgb, 0, 1))


def _platform_palette(platform: str) -> dict[str, str]:
    """Platform-branded shades: revenue (base), ad spend (darker), orders (lighter)."""
    base = PLATFORM_COLORS.get(platform, "#666666")
    return {
        "revenue": base,
        "ad_spend": _adjust_color(base, darken=0.30),
        "orders": _adjust_color(base, lighten=0.45),
    }


def _add_bar_labels(ax, bars, values, fmt_fn, min_height_frac=0.0):
    """Place value labels above bars; skip near-zero bars."""
    vals = [float(v) for v in values]
    ymax = max(vals) if vals else 1
    pad = ymax * 0.03 if ymax > 0 else 0.5
    for bar, val in zip(bars, vals):
        if val <= 0:
            continue
        if ymax > 0 and val / ymax < min_height_frac:
            continue
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() + pad,
            fmt_fn(val),
            ha="center",
            va="bottom",
            fontsize=8.5,
            fontweight="600",
            color="#1a1a1a",
        )


def _day_count_in_range(start_str: str, end_str: str) -> int:
    try:
        start_dt = datetime.strptime(start_str, "%Y-%m-%d").date()
        end_dt = datetime.strptime(end_str, "%Y-%m-%d").date()
        return (end_dt - start_dt).days + 1
    except ValueError:
        return 1


def _prepare_daily_roas(raw: pd.DataFrame) -> pd.DataFrame:
    """Daily gross ROAS per platform; NaN where ad spend is zero."""
    if raw.empty:
        return raw
    daily = raw.copy()
    daily["report_date"] = pd.to_datetime(daily["report_date"])
    daily["gross_roas"] = np.where(
        daily["ad_spend"] > 0,
        daily["gross_revenue_excl_gst"] / daily["ad_spend"],
        np.nan,
    )
    order_map = {p: i for i, p in enumerate(PLATFORM_ORDER)}
    daily["_sort"] = daily["platform"].map(order_map).fillna(99)
    return daily.sort_values(["report_date", "_sort"]).drop(columns=["_sort"])


def _plot_roas_by_day(ax, raw: pd.DataFrame) -> None:
    """Line chart: gross ROAS per channel for each day in the window."""
    daily = _prepare_daily_roas(raw)
    if daily.empty:
        ax.set_visible(False)
        return

    dates = sorted(daily["report_date"].unique())
    if len(dates) < 2:
        ax.set_visible(False)
        return

    x = np.arange(len(dates))
    date_labels = [pd.Timestamp(d).strftime("%d %b") for d in dates]

    for platform in PLATFORM_ORDER:
        plat = daily[daily["platform"] == platform]
        if plat.empty:
            continue
        series = (
            plat.set_index("report_date")["gross_roas"]
            .reindex(dates)
        )
        y = series.values.astype(float)
        if np.all(np.isnan(y)):
            continue
        color = PLATFORM_COLORS.get(platform, "#666666")
        label = PLATFORM_LABELS.get(platform, platform.title())
        marker = PLATFORM_MARKERS.get(platform, "o")
        ax.plot(
            x,
            y,
            marker=marker,
            markersize=6,
            linewidth=2.2,
            color=color,
            markerfacecolor=color,
            markeredgecolor="white",
            markeredgewidth=0.8,
            label=label,
            zorder=3,
        )
        for xi, val in zip(x, y):
            if np.isnan(val) or val <= 0:
                continue
            ax.annotate(
                f"{val:.2f}x",
                (xi, val),
                textcoords="offset points",
                xytext=(0, 7),
                ha="center",
                fontsize=7.5,
                fontweight="600",
                color=color,
            )

    ax.set_title("Gross ROAS by Channel (Daily)", fontsize=11, fontweight="bold", color="#1a1a1a", pad=10)
    ax.set_xticks(x)
    ax.set_xticklabels(date_labels, fontsize=9, rotation=0)
    ax.set_ylabel("Gross ROAS", fontsize=10, color="#333333", labelpad=8)
    ax.set_facecolor("#FAFBFC")
    ax.grid(axis="y", alpha=0.32, linestyle="-", color="#CCCCCC", zorder=0)
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    ax.spines["left"].set_color("#BBBBBB")
    ax.spines["bottom"].set_color("#BBBBBB")

    valid = daily.loc[daily["ad_spend"] > 0, "gross_roas"].dropna()
    if not valid.empty:
        ymax = float(valid.max())
        ax.set_ylim(0, max(ymax * 1.25, 0.5))
    ax.legend(
        loc="upper center",
        bbox_to_anchor=(0.5, -0.20),
        ncol=min(len(PLATFORM_ORDER), 4),
        frameon=True,
        fontsize=8,
        title="Channels",
        title_fontsize=8,
        edgecolor="#DDDDDD",
        facecolor="white",
    )


def plot_channel_performance(
    start_date: str | date | datetime,
    end_date: str | date | datetime,
    save_path: Optional[str] = None,
    brand_id: Optional[int] = None,
    period_label: Optional[str] = None,
    include_roas_trend: bool = False,
    roas_trend_days: int = 7,
) -> Optional[str]:
    """
    Grouped bar chart: revenue, ad spend, and orders by channel for the requested window.
    Optionally adds a daily gross ROAS trend panel below (last N days ending on end_date).
    """
    try:
        raw = fetch_channel_performance(start_date, end_date, brand_id=brand_id)
        if raw.empty:
            logger.warning(
                "No channel performance data for %s to %s",
                _to_date_str(start_date),
                _to_date_str(end_date),
            )
            return None

        df = aggregate_channel_performance(raw)
        if df.empty or (
            df["gross_revenue_excl_gst"].sum() == 0
            and df["ad_spend"].sum() == 0
            and df["attributed_orders"].sum() == 0
        ):
            logger.warning("All channel performance metrics zero — skipping chart.")
            return None

        platforms = [p for p in PLATFORM_ORDER if p in df["platform"].values]
        plot_df = df.set_index("platform").reindex(platforms).fillna(0).reset_index()
        channel_labels = [PLATFORM_LABELS.get(p, p.title()) for p in platforms]

        start_str = _to_date_str(start_date)
        end_str = _to_date_str(end_date)
        if period_label:
            title = f"Channel Performance — {period_label}"
        elif start_str == end_str:
            try:
                dt = datetime.strptime(end_str, "%Y-%m-%d")
                title = f"Channel Performance — {dt.strftime('%d %b %Y')}"
            except ValueError:
                title = f"Channel Performance — {end_str}"
        else:
            try:
                s_dt = datetime.strptime(start_str, "%Y-%m-%d")
                e_dt = datetime.strptime(end_str, "%Y-%m-%d")
                title = (
                    f"Channel Performance — {s_dt.strftime('%d %b')} to "
                    f"{e_dt.strftime('%d %b %Y')}"
                )
            except ValueError:
                title = f"Channel Performance — {start_str} to {end_str}"

        total_rev = float(plot_df["gross_revenue_excl_gst"].sum())
        total_spend = float(plot_df["ad_spend"].sum())
        total_orders = int(plot_df["attributed_orders"].sum())
        total_roas = total_rev / total_spend if total_spend > 0 else 0

        rev_vals = plot_df["gross_revenue_excl_gst"].values.astype(float)
        spend_vals = plot_df["ad_spend"].values.astype(float)
        order_vals = plot_df["attributed_orders"].values.astype(float)

        n = len(platforms)
        x = np.arange(n)
        bar_w = 0.24

        num_days = _day_count_in_range(start_str, end_str)
        end_dt = datetime.strptime(end_str, "%Y-%m-%d").date()
        roas_start = end_dt - timedelta(days=roas_trend_days - 1)
        if num_days >= 2:
            roas_raw = raw
        elif include_roas_trend:
            roas_raw = fetch_channel_performance(roas_start, end_dt, brand_id=brand_id)
        else:
            roas_raw = None
        show_roas_panel = roas_raw is not None and not roas_raw.empty and len(
            roas_raw["report_date"].unique()
        ) >= 2

        if show_roas_panel:
            fig = plt.figure(figsize=(13, 10), facecolor="white")
            gs = fig.add_gridspec(2, 1, height_ratios=[1.35, 1], hspace=0.42)
            ax1 = fig.add_subplot(gs[0])
            ax2 = ax1.twinx()
            ax_roas = fig.add_subplot(gs[1])
        else:
            fig, ax1 = plt.subplots(figsize=(13, 6.5), facecolor="white")
            ax2 = ax1.twinx()
            ax_roas = None

        rev_colors = [METRIC_COLORS["revenue"]] * n
        spend_colors = [METRIC_COLORS["ad_spend"]] * n
        order_colors = [_platform_palette(p)["orders"] for p in platforms]

        bars_rev = ax1.bar(
            x - bar_w,
            rev_vals,
            bar_w,
            color=rev_colors,
            edgecolor="white",
            linewidth=1.0,
            zorder=3,
        )
        bars_spend = ax1.bar(
            x,
            spend_vals,
            bar_w,
            color=spend_colors,
            edgecolor="white",
            linewidth=1.0,
            zorder=3,
        )
        bars_orders = ax2.bar(
            x + bar_w,
            order_vals,
            bar_w,
            color=order_colors,
            edgecolor="white",
            linewidth=1.0,
            alpha=0.95,
            zorder=3,
        )

        ax1.set_xticks(x)
        tick_labels = ax1.set_xticklabels(channel_labels, fontsize=11, fontweight="600")
        for tick, platform in zip(tick_labels, platforms):
            tick.set_color(PLATFORM_COLORS.get(platform, "#222222"))
        ax1.set_ylabel(
            f"Revenue / Ad Spend ({_currency_symbol()})",
            fontsize=10,
            color="#333333",
            labelpad=10,
        )
        ax2.set_ylabel("Orders", fontsize=10, color="#444444", labelpad=10)
        ax2.tick_params(axis="y", labelcolor="#444444")

        ax1.set_facecolor("#FAFBFC")
        ax1.grid(axis="y", alpha=0.32, linestyle="-", color="#CCCCCC", zorder=0)
        ax1.set_axisbelow(True)
        for spine in ("top",):
            ax1.spines[spine].set_visible(False)
        ax2.spines["top"].set_visible(False)
        ax1.spines["left"].set_color("#BBBBBB")
        ax1.spines["bottom"].set_color("#BBBBBB")
        ax2.spines["right"].set_color("#BBBBBB")

        ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: _format_inr(v)))
        ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{int(v):,}"))

        money_max = max(float(rev_vals.max()), float(spend_vals.max()), 1.0)
        order_max = max(float(order_vals.max()), 1.0)
        ax1.set_ylim(0, money_max * 1.22)
        ax2.set_ylim(0, order_max * 1.28)

        _add_bar_labels(ax1, bars_rev, rev_vals, _format_inr)
        _add_bar_labels(ax1, bars_spend, spend_vals, _format_inr)
        _add_bar_labels(ax2, bars_orders, order_vals, lambda v: f"{int(v):,}")

        ax1.set_xlim(-0.6, n - 0.4)

        subtitle = (
            f"Total revenue: {_format_inr(total_rev)}   ·   "
            f"Total ad spend: {_format_inr(total_spend)}   ·   "
            f"Total orders: {total_orders:,}   ·   "
            f"Blended gross ROAS: {total_roas:.2f}x"
        )

        fig.suptitle(title, fontsize=15, fontweight="bold", color="#1a1a1a", y=0.98 if show_roas_panel else 0.97)
        fig.text(0.5, 0.935 if show_roas_panel else 0.905, subtitle, ha="center", va="top", fontsize=10, color="#555555")

        from matplotlib.patches import Patch

        metric_handles = [
            Patch(facecolor=METRIC_COLORS["revenue"], edgecolor="white", label=METRIC_LABELS["revenue"]),
            Patch(facecolor=METRIC_COLORS["ad_spend"], edgecolor="white", label=METRIC_LABELS["ad_spend"]),
        ]
        platform_handles = [
            Patch(
                facecolor=PLATFORM_COLORS.get(p, "#666666"),
                edgecolor="white",
                linewidth=0.8,
                label=f"{PLATFORM_LABELS.get(p, p.title())} orders",
            )
            for p in platforms
        ]
        legend_y = 0.91 if show_roas_panel else 0.875
        fig.legend(
            handles=metric_handles + platform_handles,
            loc="upper center",
            bbox_to_anchor=(0.5, legend_y),
            ncol=min(len(metric_handles) + len(platform_handles), 6),
            frameon=True,
            fontsize=8.5,
            edgecolor="#DDDDDD",
            facecolor="white",
        )
        fig.text(
            0.5,
            legend_y - 0.045 if show_roas_panel else legend_y - 0.048,
            "Bar order per channel (left → right): Revenue · Ad Spend · Orders  |  ROAS lines = channel colors",
            ha="center",
            va="top",
            fontsize=8.5,
            color="#666666",
            style="italic",
        )

        if show_roas_panel and ax_roas is not None:
            _plot_roas_by_day(ax_roas, roas_raw)
            fig.subplots_adjust(top=0.88, bottom=0.10, hspace=0.38)
        else:
            plt.tight_layout(rect=[0.04, 0.06, 0.96, 0.82])
        if save_path:
            os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
            plt.savefig(save_path, dpi=200, bbox_inches="tight", facecolor="white")
            plt.close(fig)
            logger.info("Channel performance chart saved: %s", save_path)
            return save_path
        plt.close(fig)
        return None
    except Exception as e:
        logger.error("Channel performance chart error: %s", e, exc_info=True)
        return None


def plot_channel_performance_daily(
    report_date: str | date | datetime,
    save_path: Optional[str] = None,
    brand_id: Optional[int] = None,
) -> Optional[str]:
    """Single-day channel chart — daily marketing email only (report date)."""
    day_str = _to_date_str(report_date)
    try:
        dt = datetime.strptime(day_str, "%Y-%m-%d")
        label = f"Daily — {dt.strftime('%d %b %Y')}"
    except ValueError:
        label = f"Daily — {day_str}"
    return plot_channel_performance(
        day_str,
        day_str,
        save_path=save_path,
        brand_id=brand_id,
        period_label=label,
        include_roas_trend=True,
        roas_trend_days=7,
    )


def plot_channel_performance_last_7_days(
    end_date: str | date | datetime,
    save_path: Optional[str] = None,
    brand_id: Optional[int] = None,
) -> Optional[str]:
    """Rolling 7-day channel chart — WTD/MTD email only (not the daily marketing email)."""
    end_str = _to_date_str(end_date)
    end_dt = datetime.strptime(end_str, "%Y-%m-%d").date()
    start_dt = end_dt - timedelta(days=6)
    return plot_channel_performance(
        start_dt,
        end_dt,
        save_path=save_path,
        brand_id=brand_id,
        period_label=f"Last 7 Days ({start_dt.strftime('%d %b')} – {end_dt.strftime('%d %b %Y')})",
    )
