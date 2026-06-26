# Weather Campaign Opportunity Report

Daily weather-led campaign opportunity report for Indian cities, combining
Shopify sales signals with Open-Meteo forecasts to decide where monsoon/rain
campaigns should be **scaled, tested, retargeted, prepared, kept evergreen, or
paused**.

See the full design in [`../../docs/weather_report.md`](../../docs/weather_report.md).

## Build phases

| Phase | Build | Status |
|-------|-------|--------|
| 1 | `city_master.csv` + `city_alias_map.csv` | ✅ Done |
| 2 | Shopify city sales aggregation | ✅ Done |
| 3 | Open-Meteo forecast fetcher | ✅ Done |
| 4 | Weather bucket classification | ✅ Done |
| 5 | Opportunity scoring | ✅ Done |
| 6 | Final report CSV + heatmap | ✅ Done |
| 7 | Dashboard | ⏳ |
| 8 | Historical rain-sales analysis | ⏳ |

## Phase 1 — what this delivers

The foundational reference datasets and the reproducible seeder that builds
them from the curated starter workbook
(`docs/weather/india_city_master_openmeteo_starter.xlsx`).

```
weather_report/
├── README.md
├── requirements.txt
├── src/
│   ├── normalize.py          # pure text normalization (lowercase/trim/punct/space)
│   ├── seed_city_master.py   # [P1] builds the two seed CSVs from the starter workbook
│   ├── apply_overrides.py    # [P2] folds curated manual aliases into the alias map
│   ├── sales_source.py       # [P2] Shopify orders -> raw per-(date,city,state) aggregates
│   ├── canonicalize.py       # [P2] raw city -> canonical city_id (alias + fuzzy resolver)
│   ├── llm_resolver.py       # [P2] Azure OpenAI fallback bucketing for the long tail
│   ├── aggregate_sales.py    # [P2] orchestrator -> city_sales_daily.csv
│   ├── weather_source.py     # [P3] Open-Meteo batched fetch (retry/timeout)
│   ├── fetch_forecast.py     # [P3] orchestrator -> current JSON + forecast CSV
│   ├── classify_weather.py   # [P4] city-level weather buckets -> classified CSV
│   ├── score_opportunity.py  # [P5] weather+sales+tier scoring -> scored CSV
│   ├── build_report.py       # [P6] ranked report + actions + heatmap/geo PNGs
│   └── run_report.py         # one-shot pipeline (all phases in order)
├── report.cmd                # Windows wrapper: `report [flags]`
└── data/weather/
    ├── city_master.csv       # universe of monitored cities (city_id is the join key)
    ├── city_alias_map.csv     # raw Shopify city -> canonical city_id mappings
    ├── city_sales_daily.csv   # [P2] daily city sales (history-preserving)
    ├── weather/
    │   ├── current/{run_date}.json    # [P3] raw Open-Meteo payload keyed by city_id
    │   ├── forecast/{run_date}.csv    # [P3] parsed daily rows (+[P4] per-day bucket)
    │   └── classified/{run_date}.csv  # [P4] city-level signals + weather_bucket
    ├── scored/{report_date}.csv       # [P5] sub-scores + opportunity_score (ranked)
    ├── reports/                        # [P6] final ranked report + heatmap/geo PNGs
    │   ├── campaign_opportunity_{report_date}.csv
    │   ├── campaign_opportunity_{report_date}_heatmap.png
    │   └── campaign_opportunity_{report_date}_map.png
    ├── review/
    │   └── unresolved_cities.csv   # cities pending review (sorted-by-volume queue)
    └── config/
        ├── open_meteo.json          # API params + retry/timeout settings
        ├── scoring_rules.json       # weather buckets, score thresholds, weights
        ├── city_alias_overrides.csv # [P2] curated district/suburb -> metro mappings
        └── monsoon_products.yaml
```

### `city_master.csv`

Columns: `city_id`, `canonical_city`, `state`, `country`, `region`,
`city_tier`, `latitude`, `longitude`, `timezone`, `population`, `is_active`.

`population` is intentionally blank in the starter (fill later from
Census/GeoNames/SimpleMaps). `timezone` defaults to `Asia/Kolkata`.

### `city_alias_map.csv`

Columns: `alias_id`, `raw_city_normalized`, `raw_state_normalized`,
`city_id`, `canonical_city`, `match_method`, `confidence_score`, `reviewed`,
`created_at`.

Seeded from the workbook's curated alias list with `match_method=alias`,
`confidence_score=100`, `reviewed=true`. Later phases append `fuzzy`,
`state_aware_fuzzy`, `llm`, and `manual` mappings as cities are discovered.

> Design note: alias → canonical resolution is **data-driven** via this CSV,
> not hard-coded in `normalize.py`. The normalizer only does pure text cleanup.

## Phase 2 — Shopify city sales aggregation

Builds `data/weather/city_sales_daily.csv` by reading `public.shopify_orders`
(Postgres), resolving each raw shipping city to a canonical `city_id`, and
aggregating to one row per `(date, city_id)`.

**Data source** (`sales_source.py`) — aggregates `shopify_orders` joined to
`shopify_order_line_items` (units) and `customer_details_th` (distinct
customers). Excludes cancelled orders from `orders`/`revenue`/`units`; counts
them separately as `cancelled_orders`. Revenue is net-of-GST. Connection reuses
`DATABASE_URL` / `DB_*` from the main project's `.env` (same as
`database_manager.py`). India-only via a tolerant `ship_country` filter.

**Resolver** (`canonicalize.py`) — for each raw `(city, state)`:

1. exact alias lookup → `match_method=alias`, score 100
2. fuzzy (`difflib`) vs all aliases + canonical names:
   - `>= 95` → auto-map (`fuzzy`), append alias
   - `85-94` **and** state matches → auto-map (`state_aware_fuzzy`), append alias
   - `70-84` → review queue
   - `< 70` → unresolved

Auto-discovered aliases are appended to `city_alias_map.csv` with
`reviewed=false` for later human sign-off.

**Output columns** (`city_sales_daily.csv`): `date`, `city_id`,
`canonical_city`, `orders`, `revenue`, `units`, `customers`,
`cancelled_orders`, `returned_orders`, `created_at`.

Reruns are history-preserving: rows for dates in the current run are replaced,
older dates are kept (no duplicate `(date, city_id)`).

### Curated overrides + LLM fallback (resolution boosters)

Two layers raise order-resolution from ~71% to ~90%:

1. **Curated overrides** (`config/city_alias_overrides.csv`, applied by
   `apply_overrides.py`) — high-volume district/suburb→metro mappings that are
   text-distant from the canonical name (`Mumbai Suburban`→Mumbai,
   `South Delhi`→Delhi NCR, `Ranga Reddy`→Hyderabad). Written as
   `match_method=manual`, `confidence=100`, `reviewed=true`. Edit the CSV and
   re-run `apply_overrides.py` to extend.

2. **LLM fallback** (`llm_resolver.py`, `aggregate_sales.py --use-llm`) — for
   whatever alias + fuzzy still can't resolve, Azure OpenAI decides whether the
   raw value is a locality/suburb/alt-spelling of a canonical city (→ map it,
   `match_method=llm`) or a genuinely different city (→ leave in review as a
   candidate *new* city). It deliberately **abstains** rather than mis-bucket a
   distant city, preserving weather accuracy. Accepted mappings need
   `confidence >= --llm-min-confidence` (default 80). Reuses the project's
   Azure OpenAI env (`AZURE_OPENAI_API_KEY/ENDPOINT/DEPLOYMENT`).

What remains in the review queue afterwards is mostly real Tier-2/3 cities
(Ajmer, Erode, Jammu…) — candidates to promote into `city_master.csv` later.

## Phase 3 — Open-Meteo forecast fetcher

Fetches 7-day forecasts for all active `city_master` cities and writes:

- `weather/current/{run_date}.json` — raw payload keyed by `city_id`
  (`current` + full `raw_response` for audit/debug).
- `weather/forecast/{run_date}.csv` — parsed daily rows: `run_date`,
  `forecast_date`, `city_id`, `canonical_city`,
  `precipitation_probability_max`, `precipitation_sum_mm`, `rain_sum_mm`,
  `precipitation_hours`, `weather_code`, `weather_bucket` (blank — Phase 4),
  `created_at`.

`weather_source.py` batches cities into multi-coordinate calls (the API accepts
comma-separated lat/lon) with timeout + retry/backoff from `open_meteo.json`.
All 75 cities fit in a single batched request.

## Phase 4 — Weather bucket classification

`classify_weather.py` turns the raw payload into actionable **city-level**
signals by combining three windows:

| Signal | Window | Source |
|--------|--------|--------|
| `rain_now` | current obs | `current.precipitation` / `current.rain` |
| `max_rain_probability_next_72h` | next 72h | hourly `precipitation_probability` (from `now`) |
| `rainfall_next_3d_mm`, `precipitation_next_3d_mm`, `precipitation_hours_next_3d` | next 3 days | daily sums |

Primary `weather_bucket` (priority order, thresholds from `scoring_rules.json`):

1. `active_rain` — raining now
2. `heavy_rain_watch` — rainfall_next_3d ≥ 20mm OR precip_next_3d ≥ 25mm
3. `high_rain_probability` — max prob next 72h ≥ 70
4. `emerging_rain` — not raining now AND max prob next 72h ≥ 50
5. `low_weather_opportunity` — max prob next 72h < 40 AND rainfall_next_3d < 5mm
6. `moderate` — everything else (`no_data` if the city's fetch failed)

Writes `weather/classified/{run_date}.csv` (one row per city, consumed by
Phase 5 scoring) and backfills the per-day `weather_bucket` column in the
Phase 3 forecast CSV. Idempotent — re-derived from the payload each run.

## Phase 5 — Opportunity scoring

`score_opportunity.py` joins the Phase 4 classification with sales windows
(from `city_sales_daily.csv`) and city tier, scoring **every active city**:

```
opportunity_score = weather_score      * 0.50
                  + sales_score        * 0.25
                  + market_size_score  * 0.15
                  + trend_score        * 0.10
```

| Sub-score | How |
|-----------|-----|
| `weather_score` | additive: rain-now +30, 72h prob (≥80/70/50), 3d rainfall (≥30/20/10), 3d precip-hours (≥12/6); capped 100 |
| `sales_score` | `0.5*norm(orders_30d) + 0.3*norm(revenue_30d) + 0.2*norm(momentum)`, min-max 0-100 |
| `market_size_score` | Tier 1=100, Tier 2=75, Tier 3=50, else 30 |
| `trend_score` | normalized sales momentum (last-7d daily rate vs prior-7d) |

Sales windows (`orders_7d/30d/90d`, `revenue_30d`, `sales_momentum`) are
computed relative to `--report-date`. Cities with **no sales still score**
(weather + market size) and are flagged `new_opportunity_city` — these are the
test/launch candidates (e.g. a Tier-3 city in active rain with zero orders).
All weights/thresholds live in `config/scoring_rules.json`.

Writes `data/weather/scored/{report_date}.csv`, ranked by `opportunity_score`
(consumed by Phase 6 for actions, reasons, and product groups).

## Phase 6 — Final report CSV + heatmap

`build_report.py` adds the decision layer on top of the scored table and emits
the deliverables a marketer acts on.

**Action matrix** (priority order; thresholds in `scoring_rules.json →
action_rules`):

| Condition | opportunity_type |
|-----------|------------------|
| low rain + high sales | **Evergreen** (sell, skip rain creative) |
| low rain + low sales | **Pause** |
| high weather + high sales | **Scale** |
| high weather + little/no sales history + Tier 1/2 | **Test** |
| active rain + established base + soft recent sales | **Retarget** |
| emerging rain (24-72h) | **Prepare** |

`Test` vs `Retarget` is split by `retarget_min_orders_90d` (default 10): a real
customer base → Retarget; little/no history → Test. Each row gets a concrete
`recommended_action`, a `reason` citing the signals, and a
`recommended_product_group` / `recommended_products` from
`monsoon_products.yaml` (Pause/Evergreen never push monsoon SKUs).

**Outputs** (`data/weather/reports/`):
- `campaign_opportunity_{report_date}.csv` — full ranked report (29 columns).
- `campaign_opportunity_{report_date}_heatmap.png` — top-N cities × sub-scores
  (Weather / Sales / Market / Trend / Opportunity), RdYlGn.
- `campaign_opportunity_{report_date}_map.png` — lat/lon scatter, colour =
  `opportunity_score`, size = `orders_30d`.

## Usage

```bash
# from the weather_report/ directory
pip install -r requirements.txt

# Phase 1 — (re)seed the reference CSVs from the starter workbook
py src/seed_city_master.py

# Phase 2 — curated overrides, then build city_sales_daily.csv
py src/apply_overrides.py                  # fold curated aliases into the map
py src/aggregate_sales.py --days 90 --use-llm   # full run with LLM fallback
py src/aggregate_sales.py --days 30        # fuzzy/alias only (no LLM)
py src/aggregate_sales.py --dry-run        # compute + print, write nothing
py src/aggregate_sales.py --sample raw.csv # offline, read raw orders from CSV

# Phase 3 — fetch forecasts for all active cities
py src/fetch_forecast.py                    # run_date = today (IST)
py src/fetch_forecast.py --run-date 2026-06-26
py src/fetch_forecast.py --limit 5          # smoke test first 5 cities

# Phase 4 — classify weather buckets (needs Phase 3 payload for the run_date)
py src/classify_weather.py                  # run_date = today (IST)
py src/classify_weather.py --run-date 2026-06-26

# Phase 5 — score opportunities (needs Phase 4 classified + city_sales_daily)
py src/score_opportunity.py                 # report-date = today (IST)
py src/score_opportunity.py --report-date 2026-06-26

# Phase 6 — final ranked report + heatmap (needs Phase 5 scored file)
py src/build_report.py                       # report-date = today (IST)
py src/build_report.py --top 25              # cities in the heatmap
py src/build_report.py --no-plots            # CSV only
```

## The `report` command (one-shot pipeline)

`run_report.py` runs all five phases in order for one `--report-date`. On
Windows, `report.cmd` wraps it so you can run it from anywhere:

```bat
report                       :: full run, today (IST): sales -> ... -> report
report --use-llm             :: include Azure OpenAI city bucketing
report --report-date 2026-06-26
report --skip-sales --skip-forecast   :: reuse cached data (fast re-score)
report --days 30 --no-plots
```

Or call Python directly (any OS):

```bash
py src/run_report.py --use-llm
```

Steps and flags:

| Step | Skip flag | Notes |
|------|-----------|-------|
| 1. aggregate_sales | `--skip-sales` | hits Postgres (+ LLM if `--use-llm`) |
| 2. fetch_forecast | `--skip-forecast` | hits Open-Meteo |
| 3. classify_weather | `--skip-classify` | local |
| 4. score_opportunity | — | local, always runs |
| 5. build_report | — | local, always runs (`--no-plots` to skip PNGs) |

By default the pipeline **stops on the first failing step**; pass
`--continue-on-error` to push through. Skipping steps 1-3 reuses the existing
CSV/JSON artifacts, so re-scoring after a config change takes ~1s.

### Or run phases individually

```bash
py src/aggregate_sales.py --days 90 --use-llm   # refresh sales + resolve cities
py src/fetch_forecast.py                         # forecasts for all active cities
py src/classify_weather.py                       # weather buckets
py src/score_opportunity.py                      # opportunity scores
py src/build_report.py                           # final report + heatmap
```

Re-running the seeder regenerates `city_master.csv` and `city_alias_map.csv`
from the workbook (idempotent). The `review/unresolved_cities.csv` queue is
upserted by normalized `(city, state)` key.
