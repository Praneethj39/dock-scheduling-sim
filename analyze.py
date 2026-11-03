"""
Run the KPI queries in kpis.sql against data/yard.db, save the results as
CSV files, draw the charts, and check the result holds across 20 different
random seeds (so it isn't luck of one simulated month).

Run:  python analyze.py
"""

import os
import sqlite3

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import simulate

DB_PATH = "data/yard.db"
OUT = "results"
SEEDS = range(1, 21)

LABELS = {"fcfs": "First come, first served", "priority": "Perishables first",
          "spt": "Fewest pallets first", "slots": "Booked slots + perishables first"}
COLORS = {"fcfs": "#2a78d6", "priority": "#eb6834", "spt": "#1baf7a", "slots": "#eda100"}
ORDER = ["fcfs", "priority", "spt", "slots"]
INK, MUTED, GRID = "#0b0b0b", "#52514e", "#e4e3df"


def run_queries():
    """Split kpis.sql on '-- name:' markers and run each query."""
    text = open("kpis.sql").read()
    results = {}
    with sqlite3.connect(DB_PATH) as con:
        for block in text.split("-- name:")[1:]:
            name, sql = block.split("\n", 1)
            results[name.strip()] = pd.read_sql_query(sql, con)
    return results


def style(ax, title, ylabel):
    ax.set_title(title, loc="left", fontsize=12, color=INK, pad=12)
    ax.set_ylabel(ylabel, color=MUTED)
    ax.tick_params(colors=MUTED, length=0)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ["top", "right", "left"]:
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)


def chart_by_hour(by_hour):
    fig, ax = plt.subplots(figsize=(9, 4.8))
    for p in ORDER:
        d = by_hour[by_hour["policy"] == p]
        ax.plot(d["arrival_hour"], d["avg_yard_wait_min"], color=COLORS[p],
                linewidth=2, marker="o", markersize=4, label=LABELS[p])
    style(ax, "Average yard wait by hour the truck reached the gate", "Minutes")
    ax.set_xlabel("Hour of day", color=MUTED)
    ax.set_xticks(range(6, 23, 2))
    ax.legend(frameon=False, fontsize=9)
    fig.tight_layout()
    fig.savefig(f"{OUT}/wait_by_hour.png", dpi=150)
    plt.close(fig)


def chart_policy_compare(summary):
    s = summary.set_index("policy").loc[ORDER]
    x = np.arange(len(ORDER))
    fig, ax = plt.subplots(figsize=(9, 4.8))
    b1 = ax.bar(x - 0.2, s["avg_yard_wait_min"], 0.38, color="#2a78d6",
                label="Wait inside the yard")
    b2 = ax.bar(x + 0.2, s["avg_delay_vs_request_min"], 0.38, color="#eb6834",
                label="Delay vs supplier's requested time")
    for bars in (b1, b2):
        ax.bar_label(bars, fmt="%.0f", padding=3, color=INK, fontsize=9)
    ax.set_xticks(x, [LABELS[p] for p in ORDER], fontsize=9)
    style(ax, "Slots cut yard waiting, but part of the wait moves to the supplier", "Minutes (average)")
    ax.legend(frameon=False, fontsize=9)
    fig.tight_layout()
    fig.savefig(f"{OUT}/policy_comparison.png", dpi=150)
    plt.close(fig)


def chart_by_category(by_cat):
    cats = ["perishable", "dry", "bulky"]
    x = np.arange(len(cats))
    fig, ax = plt.subplots(figsize=(9, 4.8))
    for i, p in enumerate(ORDER):
        d = by_cat[by_cat["policy"] == p].set_index("category").loc[cats]
        ax.bar(x + (i - 1.5) * 0.2, d["avg_yard_wait_min"], 0.19,
               color=COLORS[p], label=LABELS[p])
    ax.set_xticks(x, ["Perishable (chilled)", "Dry FMCG", "Bulky home & utilities"])
    style(ax, "Who waits under each policy", "Average yard wait (minutes)")
    ax.legend(frameon=False, fontsize=9)
    fig.tight_layout()
    fig.savefig(f"{OUT}/wait_by_category.png", dpi=150)
    plt.close(fig)


def robustness():
    """Rerun the simulation with 20 different seeds and summarise."""
    rows = []
    for seed in SEEDS:
        trucks = simulate.make_trucks(seed=seed)
        for p in ORDER:
            df = simulate.run_policy(trucks, p, seed=seed)
            wait = df["unload_start"] - df["arrive"]
            rows.append({"seed": seed, "policy": p,
                         "avg_yard_wait": wait.mean(),
                         "avg_delay_vs_request": (df["unload_start"] - df["planned_arrival"]).mean(),
                         "perishable_wait": wait[df["category"] == "perishable"].mean(),
                         "bulky_wait": wait[df["category"] == "bulky"].mean()})
    r = pd.DataFrame(rows)
    out = r.groupby("policy").agg(["mean", "min", "max"]).drop(columns="seed").round(1)
    return r, out.loc[ORDER]


def slot_capacity_sweep(rates=(6.5, 7, 7.5, 8, 9, 10), seeds=range(1, 11)):
    """How many trucks per hour should the booking system accept?
    Too few: trucks get pushed to late slots. Too many: they queue in the yard."""
    rows = []
    for seed in seeds:
        trucks = simulate.make_trucks(seed=seed)
        base = simulate.run_policy(trucks, "fcfs", seed=seed)
        rows.append({"trucks_per_hour": "FCFS (no slots)",
                     "avg_yard_wait": (base["unload_start"] - base["arrive"]).mean(),
                     "avg_delay_vs_request": (base["unload_start"] - base["planned_arrival"]).mean()})
        for rate in rates:
            df = simulate.run_policy(trucks, "slots", seed=seed, slot_trucks_per_hour=rate)
            rows.append({"trucks_per_hour": rate,
                         "avg_yard_wait": (df["unload_start"] - df["arrive"]).mean(),
                         "avg_delay_vs_request": (df["unload_start"] - df["planned_arrival"]).mean()})
    return pd.DataFrame(rows).groupby("trucks_per_hour", sort=False).mean().round(1)


def chart_sweep(sweep):
    fcfs_delay = sweep.loc["FCFS (no slots)", "avg_delay_vs_request"]
    sweep = sweep.drop("FCFS (no slots)")
    sweep.index = sweep.index.astype(float)
    fig, ax = plt.subplots(figsize=(9, 4.8))
    x = sweep.index
    ax.plot(x, sweep["avg_yard_wait"], color="#2a78d6", linewidth=2, marker="o",
            markersize=5, label="Wait inside the yard")
    ax.plot(x, sweep["avg_delay_vs_request"], color="#eb6834", linewidth=2, marker="o",
            markersize=5, label="Delay vs supplier's requested time")
    ax.axhline(fcfs_delay, color=MUTED, linewidth=1, linestyle="--")
    ax.text(8.6, fcfs_delay + 1.5, "First come, first served (delay = yard wait)",
            color=MUTED, fontsize=8)
    style(ax, "Slot booking limit: the trade-off between yard queue and supplier delay",
          "Minutes (average, 10 seeds)")
    ax.set_xlabel("Trucks accepted per hour by the booking system", color=MUTED)
    ax.legend(frameon=False, fontsize=9, loc="lower right")
    fig.tight_layout()
    fig.savefig(f"{OUT}/slot_capacity_sweep.png", dpi=150)
    plt.close(fig)


def main():
    os.makedirs(OUT, exist_ok=True)
    res = run_queries()
    for name, df in res.items():
        df.to_csv(f"{OUT}/{name}.csv", index=False)

    summary = res["policy_summary"].merge(res["p90_wait"], on="policy")
    summary.to_csv(f"{OUT}/policy_summary.csv", index=False)
    pd.set_option("display.width", 160)
    print("=== Policy summary (seed 42, 30 days) ===")
    print(summary.set_index("policy").loc[ORDER].to_string())
    print("\n=== Wait by truck type ===")
    print(res["by_category"].pivot(index="policy", columns="category",
                                   values="avg_yard_wait_min").loc[ORDER].to_string())
    print("\n=== Share of chilled trucks waiting over 45 min (%) ===")
    pc = res["by_category"][res["by_category"]["category"] == "perishable"]
    print(pc.set_index("policy").loc[ORDER, "pct_wait_over_45min"].to_string())
    print("\n=== Dock utilisation (% of 6am-10pm window), FCFS ===")
    print(res["dock_utilisation"].query("policy == 'fcfs'").to_string(index=False))
    print("\n=== Slot push (slots policy) ===")
    print(res["slot_push"].to_string(index=False))
    print("\n=== Suppliers waiting longest under FCFS ===")
    print(res["worst_vendors"].to_string(index=False))
    print("\n=== Morning peak (FCFS), avg wait by arrival hour ===")
    print(res["by_hour"].query("policy == 'fcfs'").to_string(index=False))

    chart_by_hour(res["by_hour"])
    chart_policy_compare(summary)
    chart_by_category(res["by_category"])

    print("\n=== Robustness: 20 random seeds (mean, min, max) ===")
    raw, rob = robustness()
    raw.to_csv(f"{OUT}/robustness_runs.csv", index=False)
    rob.to_csv(f"{OUT}/robustness_summary.csv")
    print(rob.to_string())

    print("\n=== Slot booking limit sweep (10 seeds) ===")
    sweep = slot_capacity_sweep()
    sweep.to_csv(f"{OUT}/slot_capacity_sweep.csv")
    print(sweep.to_string())
    chart_sweep(sweep)

    print("\n=== What-if: a 6th dock (20 seeds, perishables-first rule) ===")
    rows = []
    for seed in SEEDS:
        trucks = simulate.make_trucks(seed=seed)
        for n in (5, 6):
            df = simulate.run_policy(trucks, "priority", seed=seed, num_docks=n)
            wait = df["unload_start"] - df["arrive"]
            rows.append({"docks": n, "avg_yard_wait": wait.mean(),
                         "p90_yard_wait": wait.quantile(0.9),
                         "pct_over_60": 100 * (wait > 60).mean()})
    extra = pd.DataFrame(rows).groupby("docks").mean().round(1)
    extra.to_csv(f"{OUT}/sixth_dock_whatif.csv")
    print(extra.to_string())
    print(f"\nCSV files and charts saved in {OUT}/")


if __name__ == "__main__":
    main()
