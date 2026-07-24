# Report ↔ Dashboard Reconciliation — 2026-07-24

Fresh reconciliation of the email/PDF reports against the stable Seleric dashboard
(`Seleric_Dashboard/Node-Backend`), across DAILY / WTD / MTD, all channels and totals.
Source of truth: `GET /v1/historical/dashboard` (canonical P&L spine
`pnlService.js` / `lineItemHistoricalSql.js`, produced upstream by Mage
`fct_daily_pnl.sql`). Reports run API-first (`USE_API_ONLY=true`, brand 20 / company 19).

Run all gates: `venv/Scripts/python.exe scripts/reconcile_all.py`

## Result matrix (2026-07-24 live data)

| Gate | Scope | Result |
|---|---|---|
| `_verify_dashboard_alignment.py` | DAILY/WTD/MTD totals, per-channel NP identity, Gross/Net/BE ROAS | PASS |
| `_verify_channel_recon.py` | channel table + residual == Total (two metric paths) | PASS |
| `_verify_daily_pdf.py` | daily PDF channel numbers == dashboard | PASS |
| `_verify_wtd_mtd_email.py` | rendered email body totals/channels/**Amazon**/residual == dashboard | PASS |
| `_verify_entity_recon.py` | entity rollups internal Σ + ad-spend vs canonical `pnl/summary` | PASS |
| `_verify_amazon_recon.py` | settlement-vs-dashboard Amazon divergence (diagnostic) | DIAG |
| `_verify_producer_gold.py` | `gold.fct_daily_pnl` consistency + freshness (diagnostic) | DIAG |

## Headline tie-outs (to the rupee)

| Window | net_sales | ad_spend | COGS | **net_profit** | Gross/Net/BE ROAS |
|---|---|---|---|---|---|
| WTD 07-20→07-23 | 752,652.63 | 328,330.36 | 293,906.68 | **130,415.59** | 2.35 / 1.40 / 1.64 |
| MTD 07-01→07-23 | 4,145,275.34 | 1,768,785.20 | 1,704,047.53 | **672,442.61** | 2.61 / 1.38 / 1.70 |

Per-channel net-profit identity (`net_sales − cogs − ad_spend`) matches the dashboard on
every channel and window (Meta / Google / Organic / Amazon). Channels + residual
reconcile to the Total.

## Changes applied (report side only — dashboard/Mage untouched)

1. **WTD/MTD email Amazon row → canonical dashboard Amazon**
   (`WTD_MTD/timerange_wtd_mtd_rollup.py`, `format_summary_for_email`).
   Previously the Amazon row rendered a *settlement/net-payout* P&L
   (`get_amazon_clickhouse_summary`, direct ClickHouse) that diverged from the dashboard
   Amazon channel by ₹6k–14k per window (sometimes sign-flipped), so the Amazon row did
   not sum into the email TOTAL. Now Amazon renders from `channels['amazon']` (the
   dashboard channel), exactly like Meta/Google/Organic, so it ties to the rupee and sums
   into the TOTAL. The settlement figure is retained only as a clearly-labelled
   "Amazon settlement context (net-payout basis)" sub-line.

2. **WTD/MTD email residual row** (same file). Added an "Other (unattributed)" row
   (`canonical total − Σ channels`) so the four channel rows visibly reconcile to the
   TOTAL (the dashboard carries an unattributed bucket; MTD residual net_profit
   ≈ −₹21,815). Shown only when non-trivial.

3. **Entity reconciliation cross-source check activated** (`entity_report.py`,
   `reconcile_entity_report`). It read snake_case keys (`meta_ads_cost`,
   `google_ads_cost`, `total_sales`) but `pnl/summary` returns **camelCase**
   (`metaAdsCost`, `googleAdsCost`, `netSalesExclTax`), so the ad-spend cross-check was
   silently skipped (source = `None`). Fixed the keys; the check now runs. Gave the
   attribution-cohort ad-spend rows a small relative tolerance (floor ₹50 / 0.05%),
   distinct from the exact internal-Σ tolerance.

## Findings (recorded, not "fixed" — by design or upstream)

- **Meta campaign cohort** intentionally sums ~6% above the Meta channel row
  (attribution cohort vs canonical channel P&L); the dashboard's own Meta Attribution
  page shows the same split. **Left as-is** (mirrors the dashboard).
- **Google entity ad-spend** MTD is ₹4.72 (0.001%) below canonical `googleAdsCost` — a
  tiny attribution-cohort completeness gap (campaign present in `fct_google_ads_daily`
  but absent from the attribution join, or paisa accumulation over 119 campaigns). Meta
  ties to the paisa. Within the cohort tolerance; not material.
- **Direct-CH `gold.fct_daily_pnl` fallback replica is stale/partial.** It is internally
  consistent (one row/date; `total_ad_spend == meta+google+amazon`) but early-window ad
  spend is under-populated (Jul 1–5 ≈ ₹500/day vs real tens of thousands), so MTD replica
  ad_spend is ~half the API. Also `net_sales_excl_tax` is Shopify-only (backend blends
  Amazon). **Operational risk:** if `USE_API_ONLY` were ever flipped to `false`, reports
  would read wrong numbers from this replica. Reports correctly stay API-first. Investigate
  the `ICEBERG_GOLD_TO_CLICKHOUSE` sync freshness on `clickhouse.seleric.com`; do NOT
  change report formulas.

## New/changed files

- New gates: `scripts/_verify_wtd_mtd_email.py`, `scripts/_verify_entity_recon.py`,
  `scripts/_verify_amazon_recon.py`, `scripts/_verify_producer_gold.py`,
  `scripts/reconcile_all.py`.
- Edited: `WTD_MTD/timerange_wtd_mtd_rollup.py` (Amazon row + residual row),
  `entity_report.py` (cross-source key fix + cohort tolerance).
