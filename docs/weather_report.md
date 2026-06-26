# Weather Campaign Opportunity Report

## Core Idea

Shopify city → normalize → canonical city bucket → lat/lon → Open-Meteo forecast → weather score → sales score → campaign action

The system should evaluate two city groups:

| City Group | Purpose |
|------------|---------|
| Sales cities | Cities already found in Shopify orders |
| Opportunity cities | Important Indian cities with no/low sales but strong rain signal |

So the report should not only say:

> Mumbai is raining and has sales.

It should also say:

> Surat has high rain probability, low sales, but good population/tier opportunity. Start a test campaign.

## Functional Requirement

Build a daily weather-led campaign opportunity report for Indian cities using:

- `shopify_orders` city/order data
- Internal India `city_master.csv`
- Open-Meteo forecast API
- Optional Open-Meteo historical API
- Campaign scoring rules

The output should rank cities where monsoon/rain products can be scaled, tested, monitored, or paused.

## Data Architecture

Use **CSV for tabular datasets** and **JSON for raw API payloads and config**. No database tables are required for the MVP.

### Directory Layout

```
data/weather/
├── city_master.csv                  # curated static reference (seed from docs/weather/india_city_master_openmeteo_starter.xlsx)
├── city_alias_map.csv               # Shopify city → canonical city mappings (append/update)
├── city_sales_daily.csv             # daily city-level sales aggregates
├── weather/
│   ├── current/{run_date}.json      # raw Open-Meteo current response per city batch
│   └── forecast/{run_date}.csv      # parsed daily forecast rows per city
├── reports/
│   └── campaign_opportunity_{report_date}.csv   # final ranked report output
├── review/
│   └── unresolved_cities.csv        # cities pending manual/LLM review
└── config/
    ├── open_meteo.json              # API params
    ├── scoring_rules.json           # score thresholds and weights
    └── monsoon_products.yaml        # product group mapping
```

**Conventions**

- Use `city_id` as the join key across all CSV files.
- Append new rows to daily files; do not overwrite history.
- Write one forecast CSV and one report CSV per run date (`YYYY-MM-DD`).
- Store full Open-Meteo responses as JSON for audit/debugging; store parsed fields in CSV for scoring.

### 1. `city_master.csv`

This is the base universe of cities to monitor. Seed from `docs/weather/india_city_master_openmeteo_starter.xlsx`, then maintain as CSV.

**Columns:** `city_id`, `canonical_city`, `state`, `country`, `region`, `city_tier`, `latitude`, `longitude`, `timezone`, `population`, `is_active`

```csv
city_id,canonical_city,state,country,region,city_tier,latitude,longitude,timezone,population,is_active
1,Mumbai,Maharashtra,India,West,Tier 1,19.0760,72.8777,Asia/Kolkata,20411000,true
2,Pune,Maharashtra,India,West,Tier 1,18.5204,73.8567,Asia/Kolkata,3120000,true
```

Example rows:

| canonical_city | state | region | city_tier |
|----------------|-------|--------|-----------|
| Mumbai | Maharashtra | West | Tier 1 |
| Pune | Maharashtra | West | Tier 1 |
| Delhi NCR | Delhi/Haryana/UP | North | Tier 1 |
| Bengaluru | Karnataka | South | Tier 1 |
| Surat | Gujarat | West | Tier 2 |
| Indore | Madhya Pradesh | Central | Tier 2 |

### 2. `city_alias_map.csv`

This avoids re-resolving the same city every day. Append new mappings as they are discovered.

**Columns:** `alias_id`, `raw_city_normalized`, `raw_state_normalized`, `city_id`, `canonical_city`, `match_method`, `confidence_score`, `reviewed`, `created_at`

```csv
alias_id,raw_city_normalized,raw_state_normalized,city_id,canonical_city,match_method,confidence_score,reviewed,created_at
1,gurgaon,haryana,4,Delhi NCR,alias,100,true,2026-06-26
2,bangalore,karnataka,5,Bengaluru,alias,100,true,2026-06-26
```

Examples:

| raw_city_normalized | canonical_city | match_method |
|---------------------|----------------|--------------|
| gurgaon | Delhi NCR | alias |
| gurugram | Delhi NCR | alias |
| new delhi | Delhi NCR | alias |
| bangalore | Bengaluru | alias |
| bombay | Mumbai | alias |
| navi mumbai | Mumbai MMR | alias |

### 3. `city_sales_daily.csv`

Daily city-level sales aggregates, appended each run.

**Columns:** `date`, `city_id`, `canonical_city`, `orders`, `revenue`, `units`, `customers`, `cancelled_orders`, `returned_orders`, `created_at`

```csv
date,city_id,canonical_city,orders,revenue,units,customers,cancelled_orders,returned_orders,created_at
2026-06-26,1,Mumbai,14,84200,18,13,1,0,2026-06-26T08:00:00+05:30
```

### 4. `weather/forecast/{run_date}.csv`

Parsed forecast rows per city per forecast day. Raw API response stored separately as JSON.

**Columns:** `run_date`, `forecast_date`, `city_id`, `canonical_city`, `precipitation_probability_max`, `precipitation_sum_mm`, `rain_sum_mm`, `precipitation_hours`, `weather_code`, `weather_bucket`, `created_at`

```csv
run_date,forecast_date,city_id,canonical_city,precipitation_probability_max,precipitation_sum_mm,rain_sum_mm,precipitation_hours,weather_code,weather_bucket,created_at
2026-06-26,2026-06-26,1,Mumbai,91,18.4,16.2,6,63,active_rain,2026-06-26T08:00:00+05:30
```

### 5. `weather/current/{run_date}.json`

Raw Open-Meteo current + hourly + daily payload keyed by `city_id`. Parsed current fields can be derived at scoring time or written to a slim CSV if needed.

```json
{
  "run_date": "2026-06-26",
  "fetched_at": "2026-06-26T08:00:00+05:30",
  "cities": {
    "1": {
      "canonical_city": "Mumbai",
      "latitude": 19.076,
      "longitude": 72.8777,
      "current": {
        "temperature_2m": 28.1,
        "relative_humidity_2m": 88,
        "precipitation": 1.2,
        "rain": 1.2,
        "weather_code": 63,
        "cloud_cover": 92,
        "wind_speed_10m": 14.4
      },
      "raw_response": {}
    }
  }
}
```

### 6. `reports/campaign_opportunity_{report_date}.csv`

Final ranked report output for dashboard, Slack, or email.

**Columns:** `report_date`, `rank`, `city_id`, `city`, `state`, `region`, `city_tier`, `weather_status`, `rain_now`, `max_rain_probability_next_72h`, `rainfall_next_3d_mm`, `precipitation_hours_next_3d`, `orders_7d`, `orders_30d`, `revenue_30d`, `sales_momentum`, `weather_score`, `sales_score`, `market_size_score`, `opportunity_score`, `opportunity_type`, `recommended_action`, `reason`, `recommended_product_group`, `existing_sales_city`, `new_opportunity_city`, `created_at`

```csv
report_date,rank,city_id,city,state,region,city_tier,weather_status,rain_now,max_rain_probability_next_72h,rainfall_next_3d_mm,orders_30d,revenue_30d,opportunity_score,recommended_action,reason
2026-06-26,1,1,Mumbai,Maharashtra,West,Tier 1,Active Rain,true,91,46,420,240000,87,Scale,Strong rain signal with proven sales
```

## City Canonicalization Logic

### Flow

```
raw city from Shopify
        ↓
normalize text
        ↓
check city_alias_map.csv
        ↓
if found → city_id
        ↓
if not found → fuzzy match against city_master.csv
        ↓
if high confidence → append row to city_alias_map.csv
        ↓
if medium confidence → state-aware validation
        ↓
if low confidence → LLM/manual review queue
```

### Normalization Examples

```python
def normalize_city(city: str) -> str:
    city = city.lower().strip()
    city = city.replace(".", " ")
    city = city.replace("-", " ")
    city = " ".join(city.split())

    replacements = {
        "gurgaon": "gurugram",
        "ggn": "gurugram",
        "blr": "bengaluru",
        "bangalore": "bengaluru",
        "bombay": "mumbai",
        "new delhi": "delhi",
        "delhi ncr": "delhi ncr"
    }

    return replacements.get(city, city)
```

### Matching Thresholds

| Confidence | Action |
|------------|--------|
| 95–100 | auto-map |
| 85–94 | auto-map if state matches |
| 70–84 | send to review |
| <70 | unresolved |

## Open-Meteo Usage

### Forecast API

Open-Meteo forecast endpoint accepts geographical coordinates and returns JSON hourly weather forecasts. It supports current, hourly, and daily variable lists.

Use this config:

```json
{
  "base_url": "https://api.open-meteo.com/v1/forecast",
  "params": {
    "latitude": "{latitude}",
    "longitude": "{longitude}",
    "current": "temperature_2m,relative_humidity_2m,precipitation,rain,weather_code,cloud_cover,wind_speed_10m",
    "hourly": "precipitation_probability,precipitation,rain,showers,weather_code,cloud_cover,relative_humidity_2m",
    "daily": "precipitation_sum,rain_sum,precipitation_probability_max,precipitation_hours,weather_code",
    "forecast_days": 7,
    "timezone": "Asia/Kolkata"
  }
}
```

### Historical API

Use this later to understand whether rain actually increases sales. Open-Meteo historical weather data supports past weather records and daily/hourly weather variables by location.

Use this for:

- rainy days vs dry days sales
- monsoon product uplift
- weather-sales lag analysis
- city-level seasonal demand

## Weather Buckets

### Active Rain

- current precipitation > 0 **OR** current rain > 0

### High Rain Probability

- max precipitation_probability in next 72h >= 70%

### Emerging Rain

- current rain = 0 **AND** max precipitation_probability in next 72h >= 50%

### Heavy Rain Watch

- rainfall_next_3d_mm >= 20 **OR** precipitation_next_3d_mm >= 25

### Low Weather Opportunity

- max precipitation_probability_next_72h < 40 **AND** rainfall_next_3d_mm < 5

## Opportunity Score

Use this first version:

```python
opportunity_score = (
    weather_score * 0.50
    + sales_score * 0.25
    + market_size_score * 0.15
    + trend_score * 0.10
)
```

### Weather Score

```python
weather_score = 0

if rain_now:
    weather_score += 30

if max_rain_probability_next_72h >= 80:
    weather_score += 30
elif max_rain_probability_next_72h >= 70:
    weather_score += 25
elif max_rain_probability_next_72h >= 50:
    weather_score += 15

if rainfall_next_3d_mm >= 30:
    weather_score += 25
elif rainfall_next_3d_mm >= 20:
    weather_score += 20
elif rainfall_next_3d_mm >= 10:
    weather_score += 10

if precipitation_hours_next_3d >= 12:
    weather_score += 15
elif precipitation_hours_next_3d >= 6:
    weather_score += 10

# Cap it
weather_score = min(weather_score, 100)
```

### Sales Score

```python
sales_score = (
    normalized_orders_30d * 0.5
    + normalized_revenue_30d * 0.3
    + normalized_sales_momentum * 0.2
)
```

### Market Size Score

```python
if city_tier == "Tier 1":
    market_size_score = 100
elif city_tier == "Tier 2":
    market_size_score = 75
elif city_tier == "Tier 3":
    market_size_score = 50
else:
    market_size_score = 30
```

## Campaign Action Logic

| Condition | Opportunity Type | Action |
|-----------|------------------|--------|
| High weather + high sales | Scale | Increase budget on rain/monsoon products |
| High weather + low/no sales + Tier 1/2 | Test | Launch city test adset |
| Rain active + past customers | Retarget | Retarget old buyers with monsoon products |
| Emerging rain in 24–72h | Prepare | Prepare creatives and budget |
| Low rain + high sales | Evergreen | Continue normal campaign, avoid rain creative |
| Low rain + low sales | Pause | Do not allocate monsoon budget |

## Final Report Output (Sample)

| Rank | City | State | Weather | Rain Now | Rain Prob 72h | Rainfall 3D | Orders 30D | Revenue 30D | Score | Action |
|------|------|-------|---------|----------|---------------|-------------|------------|-------------|-------|--------|
| 1 | Mumbai | Maharashtra | Active Rain | Yes | 91% | 46mm | 420 | ₹2.4L | 87 | Scale |
| 2 | Pune | Maharashtra | High Rain Probability | No | 84% | 31mm | 130 | ₹82K | 78 | Scale/Test |
| 3 | Surat | Gujarat | Emerging Rain | No | 76% | 22mm | 12 | ₹8K | 71 | Test |
| 4 | Indore | Madhya Pradesh | Emerging Rain | No | 69% | 16mm | 0 | ₹0 | 63 | Test Small Budget |
| 5 | Delhi NCR | Delhi NCR | Low Rain | No | 28% | 2mm | 360 | ₹3.1L | 42 | Evergreen Only |

## Dynamic Procedure

### Daily Automation

1. Extract raw Shopify city sales for last 90 days.
2. Normalize raw city names.
3. Map raw cities to canonical `city_id` using `city_alias_map.csv`.
4. Append unresolved cities to `review/unresolved_cities.csv`.
5. Load all active cities from `city_master.csv`.
6. Fetch Open-Meteo forecast for all active cities.
7. Write raw weather to `weather/current/{run_date}.json` and parsed forecast to `weather/forecast/{run_date}.csv`.
8. Classify weather bucket.
9. Join sales + weather + city tier in memory (pandas or polars).
10. Calculate opportunity score.
11. Write `reports/campaign_opportunity_{report_date}.csv`.
12. Send dashboard/Slack/WhatsApp/email summary.

## Product Mapping Layer

Since this is for monsoon campaign decisions, add product groups:

```yaml
monsoon_products:
  rain_protection:
    - StormGuard Jacket
    - WildTrail Boots
    - SnugSole Pet Boots
    - TenderTrek Silicone Boots
    - Zoomie Boots

  post_rain_care:
    - LumiRinse Spa Shower
    - FluffMist Pet Brush

  travel:
    - AeroPod Luxury Pet Carrier
    - PocketPup Tote Bag

  indoor_comfort:
    - ComfortCrib
    - LumenPool Bed
```

Then the report can recommend products:

| Weather Condition | Recommended Product Group |
|-------------------|---------------------------|
| Active rain | Raincoat, boots |
| Heavy rain forecast | Raincoat, boots, drying/grooming |
| High humidity | Grooming, skin/fur care |
| Indoor rainy days | Beds, comfort products |
| Travel disruption/rain | Carrier bags |

## Better Final Output Columns

Use these columns in the dashboard (from `reports/campaign_opportunity_{report_date}.csv`):

- `report_date`
- `rank`
- `city`
- `state`
- `region`
- `city_tier`
- `weather_status`
- `rain_now`
- `max_rain_probability_next_72h`
- `rainfall_next_3d_mm`
- `precipitation_hours_next_3d`
- `orders_7d`
- `orders_30d`
- `revenue_30d`
- `sales_momentum`
- `existing_sales_city`
- `new_opportunity_city`
- `recommended_product_group`
- `opportunity_score`
- `opportunity_type`
- `recommended_action`
- `reason`

## Cursor Implementation Prompt

Use this as your final development prompt:

---

Build a dynamic Weather Campaign Opportunity Automation Report for Indian cities using Shopify order data and Open-Meteo weather APIs.

**Objective:**  
Identify cities in India where monsoon/rain-led campaigns should be scaled, tested, monitored, or paused. The report should combine current/forecast weather signals with Shopify sales signals and a city opportunity universe.

**Core flow:**  
Shopify city data → city normalization → canonical city bucket → city lat/lon → Open-Meteo forecast → weather classification → sales join → opportunity scoring → final campaign recommendation CSV.

### Data Sources

**1. Shopify orders**

- Use `shopify_orders`.
- Extract shipping city, shipping state/province, order ID, `created_at_ist`, revenue, line items, cancellation/return indicators if available.
- Aggregate city-level orders and revenue for 7-day, 30-day, and 90-day windows.

**2. City master**

- Maintain `data/weather/city_master.csv` containing Indian cities to monitor.
- Seed from `docs/weather/india_city_master_openmeteo_starter.xlsx`.
- Required fields: `city_id`, `canonical_city`, `state`, `country`, `region`, `city_tier`, `latitude`, `longitude`, `timezone`, `population`, `is_active`.
- Include both existing sales cities and broader opportunity cities.
- Do not rely on Open-Meteo as the source of all Indian cities. Use a curated city master and use Open-Meteo geocoding only to enrich/validate coordinates.

**3. City alias map**

- Maintain `data/weather/city_alias_map.csv`.
- Store raw Shopify city values mapped to canonical `city_id`.
- Required fields: `raw_city_normalized`, `raw_state_normalized`, `city_id`, `match_method`, `confidence_score`, `reviewed`.
- Matching methods: `alias`, `fuzzy`, `state_aware_fuzzy`, `llm`, `manual`.

**4. Open-Meteo forecast**

- Use `https://api.open-meteo.com/v1/forecast`.
- Query by latitude and longitude.
- Use timezone `Asia/Kolkata`.
- Fetch 7 forecast days.
- Use:
  - `current=temperature_2m,relative_humidity_2m,precipitation,rain,weather_code,cloud_cover,wind_speed_10m`
  - `hourly=precipitation_probability,precipitation,rain,showers,weather_code,cloud_cover,relative_humidity_2m`
  - `daily=precipitation_sum,rain_sum,precipitation_probability_max,precipitation_hours,weather_code`

**5. Optional Open-Meteo historical**

- Use `https://archive-api.open-meteo.com/v1/archive`.
- Use later for rain-sales correlation: rainy days vs dry days sales, monsoon uplift, city-level product performance, weather lag analysis.

### Files to Create

1. `data/weather/city_master.csv`
2. `data/weather/city_alias_map.csv`
3. `data/weather/city_sales_daily.csv`
4. `data/weather/weather/current/{run_date}.json`
5. `data/weather/weather/forecast/{run_date}.csv`
6. `data/weather/reports/campaign_opportunity_{report_date}.csv`
7. `data/weather/review/unresolved_cities.csv`
8. `data/weather/config/open_meteo.json`, `scoring_rules.json`, `monsoon_products.yaml`

### City Canonicalization

- Normalize raw city names: lowercase, trim, remove punctuation, collapse spaces.
- Use `city_alias_map.csv` first.
- Use fuzzy match against `city_master.csv`.
- If score >= 95, auto-map and append to `city_alias_map.csv`.
- If score 85–94 and state matches, auto-map and append.
- If score 70–84, append to `review/unresolved_cities.csv`.
- If score < 70, mark unresolved in review file.
- Persist all confirmed mappings for reuse.

### Weather Classification

Create buckets:

1. **Active Rain:** current precipitation > 0 OR current rain > 0
2. **High Rain Probability:** max precipitation probability in next 72 hours >= 70
3. **Emerging Rain:** current rain = 0 AND max precipitation probability in next 72 hours >= 50
4. **Heavy Rain Watch:** rainfall next 3 days >= 20mm OR precipitation next 3 days >= 25mm
5. **Low Weather Opportunity:** max precipitation probability next 72 hours < 40 AND rainfall next 3 days < 5mm

### Opportunity Score

```
opportunity_score =
  weather_score * 0.50
  + sales_score * 0.25
  + market_size_score * 0.15
  + trend_score * 0.10
```

**Weather score:**

- rain now: +30
- probability >= 80: +30
- probability >= 70: +25
- probability >= 50: +15
- rainfall next 3 days >= 30mm: +25
- rainfall next 3 days >= 20mm: +20
- rainfall next 3 days >= 10mm: +10
- precipitation hours next 3 days >= 12: +15
- precipitation hours next 3 days >= 6: +10
- cap score at 100

### Campaign Action

- High weather + strong sales = **Scale**
- High weather + low/no sales + Tier 1/Tier 2 = **Test**
- Rain active + past customers = **Retarget**
- Emerging rain in 24–72h = **Prepare**
- Low rain + high sales = **Evergreen only**
- Low rain + low sales = **Pause/deprioritize**

### Final Report Columns

`report_date`, `rank`, `city`, `state`, `region`, `city_tier`, `weather_status`, `rain_now`, `max_rain_probability_next_72h`, `rainfall_next_3d_mm`, `precipitation_hours_next_3d`, `orders_7d`, `orders_30d`, `revenue_30d`, `sales_momentum`, `existing_sales_city`, `new_opportunity_city`, `recommended_product_group`, `opportunity_score`, `opportunity_type`, `recommended_action`, `reason`.

### Product Recommendation Logic

- Active rain: StormGuard Jacket, WildTrail Boots, SnugSole Boots, TenderTrek Boots, Zoomie Boots
- Heavy rain forecast: raincoat, boots, drying/grooming products
- High humidity: FluffMist Pet Brush, grooming products
- Rainy indoor days: ComfortCrib, LumenPool Bed
- Travel/rain disruption: AeroPod, PocketPup Tote

### Automation

- Run daily in the morning IST.
- Refresh city sales.
- Refresh weather forecasts for all active cities in `city_master.csv`.
- Recalculate opportunity scores.
- Write `reports/campaign_opportunity_{report_date}.csv`.
- Expose the latest report CSV to the dashboard.
- Add filters for city, state, region, `city_tier`, `weather_status`, `opportunity_type`, and product group.

### Engineering Requirements

- Keep API settings in config.
- Cache weather JSON by `city_id` and `run_date` under `data/weather/weather/current/`.
- Add retry and timeout handling.
- Batch multi-city weather calls where possible using comma-separated latitude/longitude.
- Keep scoring thresholds in `data/weather/config/scoring_rules.json`.
- Store raw Open-Meteo responses in JSON for audit/debugging.
- Keep unresolved city mappings in `data/weather/review/unresolved_cities.csv`.
- Use pandas/polars to read, join, and write CSVs; no database required for MVP.

---

## MVP Build Order

Build it in this order:

| Phase | Build |
|-------|-------|
| 1 | `city_master.csv` + `city_alias_map.csv` |
| 2 | Shopify city sales aggregation |
| 3 | Open-Meteo forecast fetcher |
| 4 | Weather bucket classification |
| 5 | Opportunity scoring |
| 6 | Final report CSV |
| 7 | Dashboard |
| 8 | Historical rain-sales analysis |

> **The most important MVP decision:** start with city-level rain opportunity, not product-level forecasting. Product-level intelligence can come after the city weather + sales system is stable.
