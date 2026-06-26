"""Concise weather-report insights using the opportunity mental model.

Opportunity =
  Is it raining hard?     (weather, 50%)
  + Do we sell there?     (sales, 25%)
  + Is the city big enough? (market, 15%)
  + Is demand growing?    (trend, 10%)
"""

from __future__ import annotations

import pandas as pd

_NUM_COLS = (
    "opportunity_score", "orders_30d", "orders_90d", "revenue_30d",
    "weather_score", "sales_score", "market_size_score", "trend_score",
    "sales_momentum", "max_rain_probability_next_72h", "rainfall_next_3d_mm",
)


def _prep(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in _NUM_COLS:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0)
    if "rain_now" in out.columns:
        out["rain_now"] = out["rain_now"].astype(bool)
    return out


def _city_list(names: list[str], limit: int = 3) -> str:
    names = [n for n in names if n]
    if not names:
        return ""
    if len(names) <= limit:
        return ", ".join(names)
    return ", ".join(names[:limit]) + f" +{len(names) - limit} more"


def _rain_insight(df: pd.DataFrame) -> str:
    active = int(df["rain_now"].sum()) if "rain_now" in df.columns else 0
    heavy = df[df["weather_score"] >= 70] if "weather_score" in df.columns else pd.DataFrame()
    high_prob = df[df["max_rain_probability_next_72h"] >= 70] if "max_rain_probability_next_72h" in df.columns else pd.DataFrame()

    parts: list[str] = []
    if active:
        parts.append(f"{active} cities raining now")
    if len(high_prob):
        parts.append(f"{len(high_prob)} with 70%+ rain odds in 72h")
    if not parts:
        parts.append("rain signals are mostly weak today")

    lead = _city_list(heavy.nlargest(3, "weather_score")["city"].tolist()) if not heavy.empty else ""
    tail = f" — hardest hit: {lead}." if lead else "."
    return f"Rain (50%): {'; '.join(parts)}{tail}"


def _sales_insight(df: pd.DataFrame) -> str:
    if df.empty or "orders_30d" not in df.columns:
        return "Sales (25%): no order data in this window."

    selling = df[df["orders_30d"] > 0]
    zero = len(df) - len(selling)
    if selling.empty:
        return f"Sales (25%): no orders in the last 30d across {len(df)} monitored cities."

    top = selling.nlargest(1, "orders_30d").iloc[0]
    o30 = int(top["orders_30d"])
    leader = f"{top['city']} ({o30} orders/30d)"
    if zero:
        return (
            f"Sales (25%): we already sell in {len(selling)} cities; "
            f"{zero} have zero orders — {leader} leads."
        )
    return f"Sales (25%): proven demand in {len(selling)} cities — {leader} leads."


def _market_insight(df: pd.DataFrame) -> str:
    if "city_tier" not in df.columns:
        return "Market (15%): tier data unavailable."

    tier1 = df[df["city_tier"] == "Tier 1"]
    tier2 = df[df["city_tier"] == "Tier 2"]
    # Big markets with rain worth calling out
    if "weather_score" in df.columns and "opportunity_score" in df.columns:
        big_rain = df[(df["city_tier"] == "Tier 1") & (df["weather_score"] >= 50)].nlargest(
            3, "opportunity_score"
        )
        names = _city_list(big_rain["city"].tolist())
        if names:
            return f"Market (15%): {len(tier1)} Tier-1 + {len(tier2)} Tier-2 cities — largest rain plays: {names}."

    return f"Market (15%): {len(tier1)} Tier-1 and {len(tier2)} Tier-2 cities in the monitor set."


def _trend_insight(df: pd.DataFrame) -> str:
    if "sales_momentum" not in df.columns or "trend_score" not in df.columns:
        return "Trend (10%): momentum data unavailable."

    growing = df[df["sales_momentum"] > 20]
    slowing = df[(df["orders_30d"] > 0) & (df["sales_momentum"] < -10)]

    if not growing.empty:
        hot = growing.nlargest(1, "sales_momentum").iloc[0]
        mom = float(hot["sales_momentum"])
        line = f"Trend (10%): demand rising — {hot['city']} up {mom:.0f}% vs prior week"
        if not slowing.empty:
            cold = slowing.nsmallest(1, "sales_momentum").iloc[0]
            line += f"; {cold['city']} cooling ({float(cold['sales_momentum']):.0f}%)."
        else:
            line += "."
        return line

    if not slowing.empty:
        cold = slowing.nsmallest(1, "sales_momentum").iloc[0]
        return (
            f"Trend (10%): mostly flat; {cold['city']} softening "
            f"({float(cold['sales_momentum']):.0f}% vs prior week)."
        )
    return "Trend (10%): order pace is flat vs the prior week across active cities."


def _action_insight(df: pd.DataFrame) -> str:
    if "opportunity_type" not in df.columns or df.empty:
        return ""

    top = df.iloc[0]
    action = str(top.get("opportunity_type", ""))
    city = str(top.get("city", ""))
    score = float(top.get("opportunity_score", 0))

    scale = df[df["opportunity_type"] == "Scale"]["city"].head(2).tolist()
    test = df[df["opportunity_type"] == "Test"].nlargest(2, "weather_score")["city"].tolist()

    bits: list[str] = [f"Top pick: {city} ({action}, {score:.0f})"]
    if scale:
        bits.append(f"scale {_city_list(scale, 2)}")
    if test:
        bits.append(f"test rain in {_city_list(test, 2)}")
    return "Action: " + "; ".join(bits) + "."


def generate_weather_insights(report: pd.DataFrame) -> list[str]:
    """Build short pillar-based insight bullets for the email under the graph."""
    if report is None or report.empty:
        return []

    df = _prep(report.sort_values("opportunity_score", ascending=False))

    insights = [
        _rain_insight(df),
        _sales_insight(df),
        _market_insight(df),
        _trend_insight(df),
    ]
    action = _action_insight(df)
    if action:
        insights.append(action)

    return insights
