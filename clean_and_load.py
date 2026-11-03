"""
Clean the three raw system logs, join them into one row per truck visit,
and load the result into a SQLite database (data/yard.db).

Cleaning steps
  1. Gate log: parse two different date formats, drop duplicate scans,
     map messy truck-type labels onto three standard categories
  2. Dock log: turn "D3" and "3" into the same dock number, fill in
     missing unload-end times with the typical unload time for that
     truck's pallet count, and flag those rows
  3. Join on truck_id, then run sanity checks (every booked truck has a
     gate and dock record, timestamps are in the right order, the gate's
     truck type agrees with the supplier booking)

Run:  python clean_and_load.py
"""

import os
import sqlite3

import pandas as pd

RAW_DIR = "data/raw"
DB_PATH = "data/yard.db"
FREE_TIME_MIN = 120         # trucks may wait 2 h free; after that we pay
DETENTION_RS_PER_HOUR = 400  # assumed detention charge per truck-hour

TYPE_MAP = {"perishable": "perishable", "perish.": "perishable", "chilled": "perishable",
            "dry": "dry", "dry goods": "dry", "fmcg": "dry",
            "bulky": "bulky", "h&u": "bulky", "home & utilities": "bulky"}


def parse_mixed_dates(series):
    """The gate has two scanners: one writes 2026-10-01 06:02:37,
    the handheld writes 01/10/2026 06:02. Try both formats."""
    iso = pd.to_datetime(series, format="%Y-%m-%d %H:%M:%S", errors="coerce")
    dmy = pd.to_datetime(series, format="%d/%m/%Y %H:%M", errors="coerce")
    return iso.fillna(dmy)


def clean_policy(policy):
    folder = os.path.join(RAW_DIR, policy)
    asn = pd.read_csv(os.path.join(folder, "asn_log.csv"))
    gate = pd.read_csv(os.path.join(folder, "gate_log.csv"))
    dock = pd.read_csv(os.path.join(folder, "dock_log.csv"))
    report = {"policy": policy, "asn_rows": len(asn), "gate_rows_raw": len(gate)}

    # ---- gate log
    gate["gate_in"] = parse_mixed_dates(gate["gate_in"])
    gate["gate_out"] = pd.to_datetime(gate["gate_out"])
    report["unparsed_dates"] = int(gate["gate_in"].isna().sum())
    gate = gate.sort_values("gate_in").drop_duplicates("truck_id", keep="first")
    report["duplicate_scans_removed"] = report["gate_rows_raw"] - len(gate)
    gate["gate_type"] = gate["declared_type"].str.strip().str.lower().map(TYPE_MAP)
    report["unmapped_labels"] = int(gate["gate_type"].isna().sum())

    # ---- dock log
    dock["dock_no"] = dock["dock"].astype(str).str.replace("D", "", regex=False).astype(int)
    dock["unload_start"] = pd.to_datetime(dock["unload_start"])
    dock["unload_end"] = pd.to_datetime(dock["unload_end"], errors="coerce")

    # ---- join everything on truck_id
    df = (asn.merge(gate[["truck_id", "gate_in", "gate_out", "gate_type"]], on="truck_id", how="left")
             .merge(dock[["truck_id", "dock_no", "unload_start", "unload_end"]], on="truck_id", how="left"))
    report["missing_gate_record"] = int(df["gate_in"].isna().sum())
    report["missing_dock_record"] = int(df["unload_start"].isna().sum())
    report["type_mismatch_vs_asn"] = int((df["gate_type"] != df["category"]).sum())

    # Missing unload-end: estimate from minutes-per-pallet of complete rows
    df["end_imputed"] = df["unload_end"].isna()
    done = df[~df["end_imputed"]]
    min_per_pallet = ((done["unload_end"] - done["unload_start"]).dt.total_seconds() / 60
                      / done["pallets"]).median()
    estimate = (df.loc[df["end_imputed"], "unload_start"]
                + pd.to_timedelta(df.loc[df["end_imputed"], "pallets"] * min_per_pallet, unit="m"))
    df.loc[df["end_imputed"], "unload_end"] = estimate.dt.round("s").astype(df["unload_end"].dtype)
    report["unload_end_imputed"] = int(df["end_imputed"].sum())

    # Order check: gate_in <= gate_out <= unload_start <= unload_end
    bad = ((df["gate_in"] > df["gate_out"]) | (df["gate_out"] > df["unload_start"])
           | (df["unload_start"] > df["unload_end"]))
    report["out_of_order_rows"] = int(bad.sum())

    # ---- derived columns used by the SQL queries and dashboard
    mins = lambda a, b: (df[b] - df[a]).dt.total_seconds() / 60
    df["gate_wait_min"] = mins("gate_in", "gate_out")
    df["dock_wait_min"] = mins("gate_out", "unload_start")
    df["unload_min"] = mins("unload_start", "unload_end")
    df["turnaround_min"] = mins("gate_in", "unload_end")
    df["yard_wait_min"] = mins("gate_in", "unload_start")
    df["detention_rs"] = ((df["yard_wait_min"] - FREE_TIME_MIN).clip(lower=0) / 60
                          * DETENTION_RS_PER_HOUR).round(0)
    df["visit_date"] = df["gate_in"].dt.date.astype(str)
    df["arrival_hour"] = df["gate_in"].dt.hour
    df["slot_start"] = pd.to_datetime(df["slot_start"], errors="coerce")
    df["requested_arrival"] = pd.to_datetime(df["requested_arrival"])
    # How long after the supplier's preferred time did unloading start?
    # This is the fair way to compare policies: slots cut yard waiting but
    # can push a truck to a later slot, and this number captures both.
    df["delay_vs_request_min"] = mins("requested_arrival", "unload_start")
    df["policy"] = policy

    keep = ["policy", "truck_id", "vendor_id", "category", "pallets", "requested_arrival", "slot_start",
            "gate_in", "gate_out", "dock_no", "unload_start", "unload_end", "end_imputed",
            "visit_date", "arrival_hour", "gate_wait_min", "dock_wait_min", "unload_min",
            "yard_wait_min", "turnaround_min", "delay_vs_request_min", "detention_rs"]
    return df[keep], report


def main():
    policies = sorted(os.listdir(RAW_DIR))
    frames, reports = [], []
    for p in policies:
        df, rep = clean_policy(p)
        frames.append(df)
        reports.append(rep)
    visits = pd.concat(frames, ignore_index=True)

    # store timestamps as text so SQLite can read them with its date functions
    for col in ["requested_arrival", "slot_start", "gate_in", "gate_out", "unload_start", "unload_end"]:
        visits[col] = visits[col].dt.strftime("%Y-%m-%d %H:%M:%S")

    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    with sqlite3.connect(DB_PATH) as con:
        visits.to_sql("truck_visits", con, index=False)
        pd.DataFrame(reports).to_sql("cleaning_report", con, index=False)

    print("Cleaning report")
    print(pd.DataFrame(reports).set_index("policy").T.to_string())
    print(f"\nLoaded {len(visits)} truck visits into {DB_PATH} (table truck_visits)")


if __name__ == "__main__":
    main()
