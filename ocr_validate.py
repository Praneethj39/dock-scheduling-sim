"""
OCR step 4: check every gate pass against the supplier booking.

OCR is never 100% right, and suppliers are not either. So we never trust
an OCR read on its own: we compare it with what the supplier booked
(the ASN / slot booking in truck_visits, 'slots' run).

  Everything matches       -> status "approved"  (truck goes straight in)
  Anything doesn't match   -> status "pending verification", with the reason,
                              so the guard checks only these passes by hand

Reasons we check, in this order
  unreadable field        the OCR couldn't find a field at all
  unknown truck           truck number is not in any booking
  supplier mismatch       supplier code differs from the booking
  pallet count mismatch   pallets differ from the booking
  arrived outside slot    more than 15 min before the 30-min slot starts,
                          or more than 15 min after it ends

Because we made the passes ourselves, we also know the TRUE story of each
pass (was there really a problem?). That lets us check the checker:
  - real problems caught      (good: the rules did their job)
  - false alarms              (no real problem; the OCR misread something)
  - missed                    (a real problem got approved: the dangerous one)

Output tables in data/yard.db: gate_pass_checks, ocr_validation_summary

Run:  python ocr_validate.py
"""

import sqlite3

import pandas as pd

DB_PATH = "data/yard.db"
SLOT_MIN = 30
GRACE_MIN = 15

# Join the OCR reads to the booking in SQL. LEFT JOIN keeps passes whose
# truck number isn't booked (those come back with empty booking columns).
QUERY = """
SELECT r.pass_id, r.mode,
       r.truck_id, r.vendor_id, r.arrival_time, r.pallets,
       b.truck_id  AS booked_truck,
       b.vendor_id AS booked_vendor,
       b.pallets   AS booked_pallets,
       b.slot_start
FROM ocr_reads r
LEFT JOIN truck_visits b
       ON b.truck_id = r.truck_id AND b.policy = 'slots'
"""


def outside_slot(arrival, slot_start):
    """True if the truck came too early or too late for its slot."""
    minutes = (arrival - slot_start).dt.total_seconds() / 60
    return (minutes < -GRACE_MIN) | (minutes > SLOT_MIN + GRACE_MIN)


def check(df):
    """Apply the rules. Returns a list of reasons for each pass."""
    arrival = pd.to_datetime(df["arrival_time"], errors="coerce")
    slot = pd.to_datetime(df["slot_start"], errors="coerce")
    late = outside_slot(arrival, slot)

    reasons = []
    for i, r in df.iterrows():
        why = []
        missing = [f for f in ["truck_id", "vendor_id", "arrival_time", "pallets"] if pd.isna(r[f])]
        if missing:
            why.append("unreadable field")
        if pd.notna(r["truck_id"]) and pd.isna(r["booked_truck"]):
            why.append("unknown truck")
        elif pd.notna(r["booked_truck"]):            # only compare if we found the booking
            if pd.notna(r["vendor_id"]) and r["vendor_id"] != r["booked_vendor"]:
                why.append("supplier mismatch")
            if pd.notna(r["pallets"]) and int(r["pallets"]) != int(r["booked_pallets"]):
                why.append("pallet count mismatch")
            if pd.notna(arrival[i]) and late[i]:
                why.append("arrived outside slot")
        reasons.append(why)
    return reasons


def true_issues(con):
    """What was REALLY wrong with each pass (known only because it's synthetic)."""
    truth = pd.read_sql_query(
        "SELECT g.pass_id, g.planted_issue, g.arrival_time, b.slot_start "
        "FROM ocr_ground_truth g LEFT JOIN truck_visits b "
        "ON b.truck_id = g.truck_id AND b.policy = 'slots'", con)
    late = outside_slot(pd.to_datetime(truth["arrival_time"]), pd.to_datetime(truth["slot_start"]))
    issue = truth["planted_issue"].map({"none": "", "pallet_count": "pallet count mismatch",
                                        "unknown_truck": "unknown truck"})
    issue = issue.where(~(late & (issue == "")), "arrived outside slot")
    return truth[["pass_id"]].assign(actual_issue=issue.replace("", "none"))


def main():
    with sqlite3.connect(DB_PATH) as con:
        df = pd.read_sql_query(QUERY, con)
        truth = true_issues(con)

    df["reasons"] = check(df)
    df["status"] = df["reasons"].map(lambda r: "pending verification" if r else "approved")
    df["primary_reason"] = df["reasons"].map(lambda r: r[0] if r else "")
    df["reasons"] = df["reasons"].map("; ".join)
    df = df.merge(truth, on="pass_id")

    real = df["actual_issue"] != "none"
    flagged = df["status"] != "approved"
    df["outcome"] = "correctly approved"
    df.loc[real & flagged, "outcome"] = "real problem caught"
    df.loc[~real & flagged, "outcome"] = "false alarm (OCR misread)"
    df.loc[real & ~flagged, "outcome"] = "MISSED real problem"

    summary = []
    for mode, g in df.groupby("mode"):
        summary.append({"mode": mode, "passes": len(g),
                        "auto_approved_pct": round(100 * (g["status"] == "approved").mean(), 1),
                        "pending_pct": round(100 * (g["status"] != "approved").mean(), 1),
                        "real_problems": int((g["actual_issue"] != "none").sum()),
                        **g["outcome"].value_counts().to_dict()})
    outcomes = ["correctly approved", "real problem caught",
                "false alarm (OCR misread)", "MISSED real problem"]
    summary = pd.DataFrame(summary).reindex(columns=list(summary[0])[:5] + outcomes).fillna(0)
    summary[outcomes] = summary[outcomes].astype(int)

    with sqlite3.connect(DB_PATH) as con:
        df.to_sql("gate_pass_checks", con, index=False, if_exists="replace")
        summary.to_sql("ocr_validation_summary", con, index=False, if_exists="replace")
    summary.to_csv("results/ocr_validation_summary.csv", index=False)

    pre = df[df["mode"] == "preprocessed"]
    print("Validation summary (both OCR modes)")
    print(summary.set_index("mode").T.to_string())
    print("\nPending gate passes by first reason (preprocessed OCR)")
    print(pre.loc[pre["status"] != "approved", "primary_reason"].value_counts().to_string())
    print("\nReal problems in the 200 passes:")
    print(pre["actual_issue"].value_counts().to_string())


if __name__ == "__main__":
    main()
