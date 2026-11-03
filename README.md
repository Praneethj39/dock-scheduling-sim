# Inbound Dock Scheduling Simulation & Yard Dashboard

A simulation of supplier trucks arriving at a retail distribution centre (DC),
used to compare four ways of deciding which truck gets the next free unloading
dock. It also produces the kind of messy logs a real yard has, cleans them,
runs SQL KPIs on them and shows the results on a dashboard.

## The problem in one paragraph

A DC has 5 unloading docks. About 69 supplier trucks arrive a day, but they
don't arrive evenly: suppliers dispatch overnight, so most trucks reach the
gate between 7 and 11 am. Across the whole day the docks are only about
**65% busy**, yet trucks that arrive at 10 am wait **around 100 minutes**.
So the yard doesn't have too few docks overall; it has too many trucks at
the same time. The question is which scheduling rule handles that best, and
what each rule costs.

## How to run it

You need Python 3.10 or newer.

```bash
pip install -r requirements.txt     # one time
python run_all.py                   # simulate -> clean -> analyse (~10 seconds)
streamlit run dashboard.py          # opens the dashboard in your browser
```

On a Mac, use `pip3` and `python3` if `pip` / `python` don't work.
You can also run the three steps one at a time (see below), which is the
better way to learn it.

## What each file does (read them in this order)

| Step | File | What it does |
|---|---|---|
| 1 | `simulate.py` | Creates 30 days of trucks and plays them through the yard under each of the 4 rules. Writes 3 raw log files per rule into `data/raw/`. |
| 2 | `clean_and_load.py` | Cleans the 3 logs, joins them into one row per truck, checks the data makes sense, and saves it to a SQLite database `data/yard.db`. |
| 3 | `kpis.sql` | The SQL queries that calculate the KPIs (average wait, 90th-percentile wait, wait by truck type, dock utilisation, detention cost). |
| 4 | `analyze.py` | Runs the SQL, saves CSVs and charts into `results/`, reruns the simulation with 20 different random seeds, tries different slot limits, and tests adding a 6th dock. |
| 5 | `dashboard.py` | Streamlit dashboard: pick a rule and date range, see KPI tiles, wait by hour, trucks in the yard through a day, dock utilisation, trucks past free time. |

### Step 1: the simulation, in plain words

Each truck goes through three stages: **gate check-in** (one lane, 2-6 min) ->
**wait for a free dock** -> **unload** (chilled grocery ~30 min, packaged
FMCG ~45 min, bulky home & utilities ~75 min). We use **SimPy**, a Python
library for *discrete-event simulation*: instead of ticking a clock every
second, it jumps from one event (truck arrives, dock frees up) to the next.

The **same trucks** are replayed under every rule, so the only thing that
changes between runs is the rule. That makes the comparison fair.

The four rules:

| Code name | Rule | Real-life version |
|---|---|---|
| `fcfs` | First come, first served | What most yards do by default |
| `priority` | Chilled trucks jump the dock queue | Protects the cold chain |
| `spt` | Truck with fewest pallets goes first | "Shortest job first"; quick trucks clear the queue fast |
| `slots` | Suppliers book 30-minute arrival slots, capped at what the docks can handle; chilled trucks still go first | Dock appointment system |

For `spt`, the yard doesn't know the exact unload time in advance, so it uses
the pallet count from the supplier's booking as the estimate.

In `slots`, 15% of trucks ignore their slot and arrive whenever they like,
and the rest arrive within about 12 minutes of their slot, because that's
more realistic than perfect discipline.

### Step 2: the cleaning, in plain words

A real DC records a truck in three different systems that don't agree on
formats. The simulation copies that:

| Log | Comes from | Problems we put in on purpose |
|---|---|---|
| `asn_log.csv` | Supplier booking system (ASN = Advance Shipping Notice) | None; this is the trusted source |
| `gate_log.csv` | Security gate scanner | Two date formats (`2026-10-01 06:02:37` and `01/10/2026 06:02`), double scans, 12 different spellings of the truck type ("Chilled", "perish.", "FMCG", "H&U"...) |
| `dock_log.csv` | Dock team's scanner | Dock number written as "D3" or "3"; about 1% of trucks have no unload-end scan |

What `clean_and_load.py` does about each:

- Tries both date formats and keeps whichever works
- Keeps only the first gate scan per truck (removed 38 duplicates)
- Maps every label variant onto `perishable`, `dry` or `bulky`, then checks it agrees with the supplier booking (0 mismatches)
- Fills missing unload-end times using the typical minutes-per-pallet, and **flags** those rows (`end_imputed`) so nobody mistakes an estimate for a real scan (18 per rule)
- Checks every truck's times are in order: gate in -> gate out -> unload start -> unload end (0 problems)

It prints a cleaning report, and the dashboard shows it under "Data quality".

### Step 3: the KPIs

| KPI | Meaning |
|---|---|
| Yard wait | Gate check-in to unloading start |
| Delay vs requested time | Supplier's preferred arrival to unloading start. This is the fair way to judge slots, because a slot can move a truck later than the supplier wanted. |
| 90th-percentile wait (P90) | 9 out of 10 trucks wait less than this. Shows the bad days that an average hides. SQLite has no percentile function, so the query ranks rows with `ROW_NUMBER()`, a window function. |
| Chilled trucks waiting >45 min | Cold-chain risk |
| Dock utilisation | Share of the 6 am - 10 pm window a dock spent unloading |
| Detention cost | Trucks get 2 hours free; after that we assume ₹400 per truck-hour (an assumption, not a real tariff) |

## Results

All numbers are averages over **20 random seeds** (20 different simulated
months), so they aren't the luck of one run. The single-month numbers
(seed 42) used by the dashboard are in `results/policy_summary.csv`.

| Rule | Avg yard wait | Delay vs requested time | Chilled truck wait | Bulky truck wait |
|---|---|---|---|---|
| First come, first served | 61.7 min | 61.7 min | 61.4 min | 61.9 min |
| Perishables first | 55.2 min | 55.2 min | **12.7 min** | 73.8 min |
| Fewest pallets first | 42.7 min | 42.7 min | 15.2 min | **104.9 min** |
| Booked slots (6.5 trucks/hour) | **20.1 min** | **71.9 min** | 10.2 min | 24.5 min |

![Wait by hour](results/wait_by_hour.png)

### What the results mean

1. **Perishables first** is almost free for chilled goods: their wait drops
   79% (61 -> 13 min) and the share of chilled trucks waiting over 45 minutes
   falls from about half to almost none. The cost is that dry and bulky
   trucks wait about 19% longer.

2. **Fewest pallets first** has the best average of the three queue rules
   (-31%), but it does it by making big trucks wait: bulky trucks wait 69%
   longer (62 -> 105 min) and detention cost goes *up*, because those
   trucks go past their 2-hour free time. A better average isn't always a
   better yard.

3. **Booked slots** look amazing on yard wait (-67%), but the delay versus the
   time the supplier wanted goes *up* (62 -> 72 min). The booking system
   pushes morning trucks into later slots, so the waiting happens
   on the road or at the supplier instead of in our yard. Rearranging when
   trucks come can't create dock capacity.

   ![Policy comparison](results/policy_comparison.png)

4. **So how many trucks should each hour's slots accept?** `analyze.py`
   tries 6.5 to 10 trucks per hour. Accept too few and suppliers get pushed
   late; too many and the yard queue comes back. At **7.5 trucks/hour**,
   the average yard wait is **34 min instead of 62 (-46%)** while suppliers
   are delayed no more than under first come, first served (61.5 vs 61.8 min).
   That's the balanced setting.

   ![Slot sweep](results/slot_capacity_sweep.png)

5. **What is a 6th dock worth?** With perishables first, adding a 6th dock
   cuts the average yard wait from 55 to 29 minutes and the P90 from 153 to
   87. That's the number to put next to the cost of building and staffing a
   dock (or a temporary morning-only dock team).

6. Under first come, first served, the suppliers who wait longest are the
   ones arriving around 10-11 am on average (see `results/worst_vendors.csv`).
   They are the first suppliers to move onto slots.

### Recommendation

Start with **perishables first** (no cost, protects the cold chain), then
roll out **slots at about 7.5 trucks/hour**, starting with the suppliers who
arrive in the 9-11 am peak. Only consider a 6th dock if the slot system can't
shift enough trucks out of the morning.

## Assumptions and limits (be ready to say these yourself)

- The data is simulated, not from a real DC. The arrival pattern, unload
  times and ₹400/hour detention charge are reasonable guesses, and they're all
  set at the top of `simulate.py` and `clean_and_load.py` so you can change them.
- Docks don't break down and every dock can take every truck type. A real
  yard might have chilled-only docks.
- The gate has one lane. It adds about 5 minutes on average (22 at worst), so
  the docks, not the gate, are the bottleneck here.
- Slot behaviour (15% ignore their slot, the rest are within ~12 minutes) is
  an assumption. If more suppliers ignore slots, the benefit shrinks.
- Each policy is a fixed rule. Real yard managers also use judgment.

## Possible interview questions, with plain answers

**Why a simulation and not just a formula?**
Queueing formulas assume trucks arrive at a steady average rate. Here
arrivals bunch up in the morning and unload times differ by truck type, so
a formula would underestimate the morning queue. A simulation copies what
actually happens, truck by truck.

**Why run 20 seeds?**
One simulated month could be unusually good or bad. Running 20 different
months and averaging shows whether a result is real. For example, FCFS
average wait ranged from 47 to 71 minutes across the 20 months, so a single
run could easily mislead.

**Why is the average not enough?**
Fewest-pallets-first had a good average but made bulky trucks wait 105
minutes. That's why I also tracked P90, wait by truck type and detention cost.

**How did you check the cleaned data was right?**
Every booked truck had to have one gate and one dock record, times had to be
in order, and the gate's truck type had to agree with the supplier booking.
All three checks came back clean. Estimated values are flagged, not hidden.

**What would you need to build this for a real plant?**
Live data from the gate system, weighbridge, and dock or unloading area
(often already in a plant historian or ERP), the real arrival pattern and
unload times from a few weeks of history to calibrate the model, and a way
for suppliers or transporters to book slots.

**What would you do next?**
Calibrate with real data, add docks that only handle certain truck types,
model breakdowns, and test what happens when more suppliers ignore their slots.

## Words you might be asked about

- **Discrete-event simulation**: a model that jumps from event to event (arrival, start, finish) instead of ticking through every second
- **SimPy `PriorityResource`**: a set of identical servers (our docks) where the waiting line is sorted by a priority number; a lower number goes first
- **Non-preemptive**: once a truck starts unloading, nobody can kick it off the dock, even a chilled truck
- **Detention**: a charge for keeping a truck waiting past its free time
- **ASN (Advance Shipping Notice)**: the supplier's message saying what's on the truck and when it's coming
- **Window function**: a SQL function that looks at other rows in a group without collapsing them, e.g. `ROW_NUMBER() OVER (PARTITION BY policy ORDER BY wait)`

## Putting it on GitHub

The resume links to `github.com/Praneethj39/dock-scheduling-sim`, so create a
repo with exactly that name, then from inside this folder:

```bash
git init
git add .
git commit -m "Dock scheduling simulation and yard dashboard"
git branch -M main
git remote add origin https://github.com/Praneethj39/dock-scheduling-sim.git
git push -u origin main
```
