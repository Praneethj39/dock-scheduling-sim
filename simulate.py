"""
Inbound dock scheduling simulation for a retail distribution centre (DC).

Supplier trucks arrive at the DC gate, check in, wait for a free unloading
dock, unload, and leave. We replay the SAME 30 days of trucks under four
different scheduling rules and compare how long trucks wait.

Policies
  fcfs      - first come, first served (what most yards do today)
  priority  - perishable (chilled grocery) trucks jump the dock queue
  spt       - shortest unload first (fewest pallets go first)
  slots     - suppliers book 30-minute arrival slots that spread trucks
              evenly over the day, plus perishable priority at the dock

The simulation writes three raw log files per policy, the way three separate
systems in a real DC would record them:
  asn_log.csv   - supplier booking system (Advance Shipping Notice)
  gate_log.csv  - security gate check-in scanner
  dock_log.csv  - dock team's unloading scanner
These logs are deliberately a bit messy (mixed date formats, duplicate gate
scans, inconsistent labels, a few missing unload-end scans) so the next
step, clean_and_load.py, has real cleaning to do.

Run:  python simulate.py
"""

import os
import random

import numpy as np
import pandas as pd
import simpy

# ---------------------------------------------------------------- settings
SEED = 42
DAYS = 30
NUM_DOCKS = 5
START_DATE = pd.Timestamp("2026-10-01")

# Expected number of trucks arriving in each hour of the day (6 am - 8 pm).
# Suppliers dispatch overnight, so arrivals bunch up in the morning.
HOURLY_ARRIVALS = {6: 4, 7: 9, 8: 11, 9: 10, 10: 7, 11: 5, 12: 4,
                   13: 4, 14: 4, 15: 4, 16: 3, 17: 2, 18: 2, 19: 1}

# Truck types: share of trucks, average unload time (minutes)
CATEGORIES = {
    "perishable": {"share": 0.30, "unload_mean": 30},   # chilled grocery
    "dry":        {"share": 0.50, "unload_mean": 45},   # packaged FMCG
    "bulky":      {"share": 0.20, "unload_mean": 75},   # home & utilities
}
UNLOAD_CV = 0.30            # spread of unload times (30% of the mean)
GATE_MINUTES = (2, 3, 6)    # gate check-in time: min, most likely, max

# Appointment-slot settings (used only by the "slots" policy)
SLOT_MINUTES = 30
SLOT_DAY_START, SLOT_DAY_END = 6, 20   # bookable hours
SLOT_ON_TIME_SD = 12                   # how early/late trucks show up (min)
SLOT_NO_SHOW_RATE = 0.15               # share of trucks ignoring their slot

POLICIES = ["fcfs", "priority", "spt", "slots"]
OUT_DIR = "data/raw"


# ---------------------------------------------------------------- trucks
def make_trucks(seed=SEED, days=DAYS):
    """Create the list of trucks (same list is reused for every policy)."""
    rng = np.random.default_rng(seed)
    vendors = [f"V{n:03d}" for n in range(1, 41)]
    names = list(CATEGORIES)
    shares = [CATEGORIES[c]["share"] for c in names]

    trucks = []
    for day in range(days):
        for hour, rate in HOURLY_ARRIVALS.items():
            for _ in range(rng.poisson(rate)):
                minute = day * 1440 + hour * 60 + rng.uniform(0, 60)
                cat = rng.choice(names, p=shares)
                mean = CATEGORIES[cat]["unload_mean"]
                # gamma distribution keeps unload times positive and skewed
                shape = 1 / UNLOAD_CV ** 2
                unload = rng.gamma(shape, mean / shape)
                trucks.append({
                    "day": day,
                    "planned_arrival": minute,
                    "category": cat,
                    "vendor_id": rng.choice(vendors),
                    "pallets": int(max(4, round(unload / 2.5 + rng.normal(0, 2)))),
                    "unload_min": unload,
                    "gate_min": rng.triangular(*GATE_MINUTES),
                })
    trucks.sort(key=lambda t: t["planned_arrival"])
    for i, t in enumerate(trucks, start=1):
        t["truck_id"] = f"T{i:05d}"
    return trucks


def assign_slots(trucks, seed=SEED, trucks_per_hour=None):
    """
    Slot policy: each day's trucks are booked into 30-minute slots in the
    order the suppliers asked for them, but each slot only takes as many
    trucks as the docks can roughly handle. Overflow is pushed to the next
    open slot. Most trucks then arrive close to their slot time.
    """
    rng = np.random.default_rng(seed + 1)
    if trucks_per_hour is None:
        # default: book exactly what the docks can unload in an hour
        avg_unload = sum(c["share"] * c["unload_mean"] for c in CATEGORIES.values())
        trucks_per_hour = NUM_DOCKS * 60 / avg_unload
    per_slot = trucks_per_hour * SLOT_MINUTES / 60

    def slot_capacity(s):
        # spreads a fractional capacity (e.g. 3.2 per slot) across slots
        return int((s + 1) * per_slot) - int(s * per_slot)

    out = []
    for day in sorted({t["day"] for t in trucks}):
        todays = [dict(t) for t in trucks if t["day"] == day]
        used = {}
        first_slot = SLOT_DAY_START * 60 // SLOT_MINUTES
        last_slot = SLOT_DAY_END * 60 // SLOT_MINUTES - 1
        for t in todays:
            wanted = int((t["planned_arrival"] - day * 1440) // SLOT_MINUTES)
            s = max(wanted, first_slot)
            while used.get(s, 0) >= slot_capacity(s) and s < last_slot:
                s += 1
            used[s] = used.get(s, 0) + 1
            t["slot_start"] = day * 1440 + s * SLOT_MINUTES
            if rng.random() < SLOT_NO_SHOW_RATE:
                t["arrival"] = t["planned_arrival"]        # ignored the slot
            else:
                t["arrival"] = t["slot_start"] + rng.normal(SLOT_MINUTES / 2,
                                                            SLOT_ON_TIME_SD)
            out.append(t)
    out.sort(key=lambda t: t["arrival"])
    return out


# ---------------------------------------------------------------- simulation
def dock_priority(truck, policy):
    """Lower number = served first. FCFS gives everyone the same priority,
    so SimPy falls back to arrival order."""
    if policy in ("priority", "slots"):
        return 0 if truck["category"] == "perishable" else 1
    if policy == "spt":
        # The yard can't know the exact unload time in advance, so we use
        # the pallet count from the supplier's booking as the estimate.
        return truck["pallets"]
    return 0


def run_policy(trucks, policy, seed=SEED, slot_trucks_per_hour=None, num_docks=NUM_DOCKS):
    """Simulate one policy. Returns one row per truck with all timestamps."""
    random.seed(seed)
    env = simpy.Environment()
    gate = simpy.Resource(env, capacity=1)
    docks = simpy.PriorityResource(env, capacity=num_docks)
    free_docks = list(range(1, num_docks + 1))
    records = []

    if policy == "slots":
        trucks = assign_slots(trucks, seed, slot_trucks_per_hour)
    else:
        trucks = [dict(t, arrival=t["planned_arrival"], slot_start=None)
                  for t in trucks]

    def truck_process(t):
        yield env.timeout(max(0, t["arrival"] - env.now))
        arrive = env.now
        with gate.request() as g:
            yield g
            yield env.timeout(t["gate_min"])
        gate_out = env.now
        with docks.request(priority=dock_priority(t, policy)) as d:
            yield d
            dock_no = free_docks.pop(0)
            start = env.now
            yield env.timeout(t["unload_min"])
            free_docks.append(dock_no)
        records.append({**t, "arrive": arrive, "gate_out": gate_out,
                        "unload_start": start, "unload_end": env.now,
                        "dock_no": dock_no})

    for t in trucks:
        env.process(truck_process(t))
    env.run()

    df = pd.DataFrame(records).sort_values("arrive").reset_index(drop=True)
    df["policy"] = policy
    return df


# ---------------------------------------------------------------- messy logs
def to_time(minutes):
    return START_DATE + pd.to_timedelta(minutes, unit="m")


def write_raw_logs(df, policy, seed=SEED):
    """Write the three system logs for one policy, with realistic mess."""
    rng = np.random.default_rng(seed + 7)
    folder = os.path.join(OUT_DIR, policy)
    os.makedirs(folder, exist_ok=True)

    # 1) Supplier booking system: clean, trusted source of truck category
    asn = pd.DataFrame({
        "truck_id": df["truck_id"],
        "vendor_id": df["vendor_id"],
        "category": df["category"],
        "pallets": df["pallets"],
        # the time the supplier originally wanted to arrive
        "requested_arrival": [to_time(m).strftime("%Y-%m-%d %H:%M") for m in df["planned_arrival"]],
        "slot_start": [to_time(s).strftime("%Y-%m-%d %H:%M") if pd.notna(s)
                       else "" for s in df["slot_start"]],
    })
    asn.to_csv(os.path.join(folder, "asn_log.csv"), index=False)

    # 2) Gate scanner: older system, messy
    label_variants = {"perishable": ["Perishable", "PERISHABLE", "perish.", "Chilled"],
                      "dry": ["Dry", "DRY", "dry goods", "FMCG"],
                      "bulky": ["Bulky", "BULKY", "H&U", "home & utilities"]}
    rows = []
    for _, r in df.iterrows():
        t = to_time(r["arrive"])
        # about a third of scans come from a handheld that uses dd/mm/yyyy
        when = (t.strftime("%d/%m/%Y %H:%M") if rng.random() < 0.33
                else t.strftime("%Y-%m-%d %H:%M:%S"))
        rows.append({"truck_id": r["truck_id"],
                     "gate_in": when,
                     "declared_type": rng.choice(label_variants[r["category"]]),
                     "gate_out": to_time(r["gate_out"]).strftime("%Y-%m-%d %H:%M:%S")})
        if rng.random() < 0.02:                    # double scan at the gate
            rows.append(dict(rows[-1]))
    pd.DataFrame(rows).to_csv(os.path.join(folder, "gate_log.csv"), index=False)

    # 3) Dock scanner: dock numbers written two ways, a few missing end scans
    dock = pd.DataFrame({
        "truck_id": df["truck_id"],
        "dock": [f"D{d}" if rng.random() < 0.5 else str(d) for d in df["dock_no"]],
        "unload_start": [to_time(m).strftime("%Y-%m-%d %H:%M:%S") for m in df["unload_start"]],
        "unload_end": [to_time(m).strftime("%Y-%m-%d %H:%M:%S") for m in df["unload_end"]],
    })
    missing = rng.random(len(dock)) < 0.01         # operator forgot to scan out
    dock.loc[missing, "unload_end"] = ""
    dock.to_csv(os.path.join(folder, "dock_log.csv"), index=False)


def main():
    trucks = make_trucks()
    print(f"Generated {len(trucks)} trucks over {DAYS} days "
          f"({len(trucks) / DAYS:.0f} per day, {NUM_DOCKS} docks)")
    for policy in POLICIES:
        df = run_policy(trucks, policy)
        write_raw_logs(df, policy)
        wait = df["unload_start"] - df["gate_out"]
        print(f"  {policy:9s} average dock wait {wait.mean():5.1f} min")
    print(f"Raw logs written to {OUT_DIR}/<policy>/")


if __name__ == "__main__":
    main()
