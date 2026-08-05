"""Emit event vs placement daily NP JSON for the canvas."""
import json
from dotenv import load_dotenv

load_dotenv(".env")

import pandas as pd
from timeframe_config import get_timeframe_config
from api_data_fetcher import fetch_net_profit_series_from_api
from channel_performance import fetch_channel_performance

tf = get_timeframe_config()
end = tf["end_date"]
start = end - pd.Timedelta(days=29)
s, e = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

event = fetch_net_profit_series_from_api(s, e)
ch = fetch_channel_performance(s, e, brand_id=20)
dates = pd.date_range(s, e, freq="D")
labs = [f"{d.day} {d.strftime('%b')}" for d in dates]

ev = (
    event.set_index(pd.to_datetime(event["sale_date"]))["net_profit"]
    .reindex(dates)
    .fillna(0)
    .astype(float)
)

meta, google, amazon, ads, organic = [], [], [], [], []
for d in dates:
    day = ch[pd.to_datetime(ch["report_date"]) == d]

    def g(p: str) -> float:
        sub = day[day["platform"] == p]
        return float(sub["net_profit"].sum()) if not sub.empty else 0.0

    m, ggl, a, o = g("meta"), g("google"), g("amazon"), g("organic")
    meta.append(round(m, 2))
    google.append(round(ggl, 2))
    amazon.append(round(a, 2))
    organic.append(round(o, 2))
    ads.append(round(m + ggl + a, 2))

evl = [round(float(x), 2) for x in ev.values]
print(
    json.dumps(
        {
            "start": s,
            "end": e,
            "labels": labs,
            "event": evl,
            "meta": meta,
            "google": google,
            "amazon": amazon,
            "organic": organic,
            "ads_placement": ads,
            "event_total": round(sum(evl), 2),
            "ads_total": round(sum(ads), 2),
            "organic_total": round(sum(organic), 2),
        }
    )
)
