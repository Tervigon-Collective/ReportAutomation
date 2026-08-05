from pathlib import Path

path = Path(__file__).resolve().parents[1] / "plots.py"
text = path.read_text(encoding="utf-8")
start = text.index("def plot_daily_net_profit_dual_cohort(save_path=None):")
end = text.index("\ndef plot_hourly_sales_last_7_days(save_path=None):")

new = '''def plot_daily_net_profit_dual_cohort(save_path=None):
    """
    Single figure with both Net Profit views for the last 30 days:

    1. Order-date cohort — gross/returns/cancels/COGS of orders placed that day
       (+ that day's ad spend). Returns/cancels stay on the order's day.
    2. Placement cohort — same order-date logic split Meta / Google / Amazon.
    """
    try:
        from matplotlib.ticker import FuncFormatter
        from timeframe_config import get_timeframe_config
        from dashboard_stats import (
            fetch_order_date_cohort_pnl_series,
            fetch_order_date_cohort_rows,
        )

        logger.info("Plotting dual-cohort daily net profit (order-date + placement)...")
        tf = get_timeframe_config()
        end_date = tf["end_date"]
        start_date = end_date - pd.Timedelta(days=29)
        start_str = start_date.strftime("%Y-%m-%d")
        end_str = end_date.strftime("%Y-%m-%d")
        brand_id = int(os.getenv("CLICKHOUSE_BRAND_ID", "20"))
        all_dates = pd.date_range(start_str, end_str, freq="D")
        n_days = len(all_dates)

        event_df = fetch_order_date_cohort_pnl_series(brand_id, start_str, end_str)
        if event_df is None or event_df.empty:
            logger.error("No order-date cohort net profit series for dual plot")
            return None
        event_df = event_df.copy()
        event_df["sale_date"] = pd.to_datetime(event_df["sale_date"])
        event_series = (
            event_df.set_index("sale_date")["net_profit"]
            .reindex(all_dates)
            .fillna(0.0)
            .astype(float)
        )

        channel_df = fetch_order_date_cohort_rows(brand_id, start_str, end_str)
        if channel_df is None or channel_df.empty:
            logger.error("No order-date placement rows for dual plot")
            return None
        channel_df = channel_df.copy()
        channel_df["report_date"] = pd.to_datetime(channel_df["report_date"])

        fig_w = max(12, min(16, 0.35 * n_days + 6))
        fig, (ax_event, ax_place) = plt.subplots(
            2,
            1,
            figsize=(fig_w, 11),
            facecolor="white",
            sharex=True,
            gridspec_kw={"hspace": 0.32, "top": 0.93, "bottom": 0.10, "left": 0.08, "right": 0.98},
        )

        pos_color = PLOT_COLORS["profit_pos"]
        neg_color = PLOT_COLORS["profit_neg"]
        dates_series = pd.Series(all_dates)
        net_profits = event_series.values.tolist()

        _np_line_color_segments(ax_event, dates_series, net_profits, pos_color, neg_color)
        neg = event_series < 0
        pos = event_series >= 0
        ax_event.scatter(
            all_dates[pos],
            event_series[pos],
            color=pos_color,
            marker="o",
            s=28,
            linewidths=0.5,
            edgecolors="#15803D",
            zorder=2,
        )
        ax_event.scatter(
            all_dates[neg],
            event_series[neg],
            color=neg_color,
            marker="o",
            s=28,
            linewidths=0.5,
            edgecolors="#B91C1C",
            zorder=2,
        )
        ax_event.axhline(0, color="#999999", linewidth=0.9, zorder=0)
        ax_event.set_ylabel("Net Profit (Rs)", fontsize=10, color=PLOT_COLORS["text"])
        ax_event.set_title(
            f"Daily Net Profit — order-date cohort ({start_str} to {end_str})",
            fontsize=13,
            fontweight="bold",
            color="#1a1a2e",
            pad=10,
        )
        ax_event.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f"Rs{x:,.0f}"))
        ax_event.set_facecolor("#FAFBFC")
        _apply_light_grid(ax_event)
        for spine in ("top", "right"):
            ax_event.spines[spine].set_visible(False)

        plotted = False
        for platform in ("meta", "google", "amazon"):
            plat = channel_df[channel_df["platform"] == platform]
            series = (
                plat.groupby("report_date")["net_profit"]
                .sum()
                .reindex(all_dates)
                .fillna(0.0)
            )
            if series.abs().sum() == 0:
                continue
            plotted = True
            color = PLATFORM_COLORS.get(platform, "#666666")
            label = PLATFORM_LABELS.get(platform, platform.title())
            ax_place.plot(
                all_dates,
                series.values,
                color=color,
                marker="o",
                markersize=3.5,
                linewidth=2.0,
                markerfacecolor=color,
                markeredgecolor="white",
                markeredgewidth=0.6,
                label=label,
                zorder=3,
            )

        if not plotted:
            logger.error("Placement panel empty in dual-cohort plot")
            plt.close(fig)
            return None

        ax_place.axhline(0, color="#999999", linewidth=0.9, zorder=1)
        ax_place.set_xlabel("Date", fontsize=10, color=PLOT_COLORS["text"])
        ax_place.set_ylabel("Net Profit (Rs)", fontsize=10, color=PLOT_COLORS["text"])
        ax_place.set_title(
            f"Daily Net Profit — order-date by placement ({start_str} to {end_str})",
            fontsize=13,
            fontweight="bold",
            color="#1a1a2e",
            pad=10,
        )
        ax_place.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f"Rs{x:,.0f}"))
        ax_place.set_facecolor("#FAFBFC")
        _apply_light_grid(ax_place)
        for spine in ("top", "right"):
            ax_place.spines[spine].set_visible(False)
        ax_place.legend(
            loc="upper left",
            fontsize=9,
            frameon=True,
            edgecolor="#DDDDDD",
            facecolor="white",
            title="Placement",
        )

        for ax in (ax_event, ax_place):
            ax.xaxis.set_major_locator(mdates.DayLocator(interval=1))
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
            plt.setp(
                ax.xaxis.get_majorticklabels(),
                rotation=90,
                ha="center",
                fontsize=7 if n_days > 20 else 8,
                color=PLOT_COLORS["text"],
            )
            ax.spines["left"].set_color("#BBBBBB")
            ax.spines["bottom"].set_color("#BBBBBB")

        fig.suptitle(
            "Daily Net Profit — order-date cohort (returns stay with the order day)",
            fontsize=14,
            fontweight="bold",
            color="#1a1a2e",
            y=0.985,
        )
        fig.text(
            0.5,
            0.015,
            "For orders placed on day D: gross − returns/cancels of those orders − net COGS − day D ad spend. "
            "Not event-date returns dumped onto D.",
            ha="center",
            fontsize=8,
            color="#666666",
        )

        if save_path:
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            plt.savefig(save_path, dpi=200, bbox_inches="tight")
            logger.info("Dual-cohort net profit plot saved as: %s", save_path)
            plt.close(fig)
            return save_path
        plt.close(fig)
        return None
    except Exception as e:
        logger.error("Error plotting dual-cohort net profit: %s", e, exc_info=True)
        return None


'''

path.write_text(text[:start] + new + text[end:], encoding="utf-8")
print("patched", path)
