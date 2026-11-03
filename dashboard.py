"""
Yard monitoring dashboard (Streamlit), reading the cleaned data in data/yard.db.

Run:  streamlit run dashboard.py
Then open the link it prints (usually http://localhost:8501).
"""

import sqlite3

import pandas as pd
import streamlit as st

DB_PATH = "data/yard.db"
LABELS = {"fcfs": "First come, first served", "priority": "Perishables first",
          "spt": "Fewest pallets first", "slots": "Booked slots + perishables first"}


@st.cache_data
def load():
    with sqlite3.connect(DB_PATH) as con:
        df = pd.read_sql_query("SELECT * FROM truck_visits", con)
        report = pd.read_sql_query("SELECT * FROM cleaning_report", con)
    for c in ["gate_in", "unload_start", "unload_end"]:
        df[c] = pd.to_datetime(df[c])
    return df, report


def trucks_waiting_by_minute(day_df):
    """Number of trucks inside the yard but not yet at a dock, minute by minute."""
    start = day_df["gate_in"].min().floor("h")
    end = day_df["unload_start"].max().ceil("h")
    minutes = pd.date_range(start, end, freq="5min")
    counts = [((day_df["gate_in"] <= m) & (day_df["unload_start"] > m)).sum() for m in minutes]
    return pd.DataFrame({"time": minutes, "trucks waiting": counts}).set_index("time")


st.set_page_config(page_title="Yard dashboard", layout="wide")
st.title("Inbound yard dashboard")
df, report = load()

# ---- filters, one row
c1, c2 = st.columns([2, 3])
policy = c1.selectbox("Scheduling policy", list(LABELS), format_func=LABELS.get)
dates = sorted(df["visit_date"].unique())
day_range = c2.select_slider("Dates", options=dates, value=(dates[0], dates[-1]))
view = df[(df["policy"] == policy) & df["visit_date"].between(*day_range)]

# ---- KPI tiles
k = st.columns(5)
k[0].metric("Trucks", f"{len(view):,}")
k[1].metric("Avg yard wait", f"{view['yard_wait_min'].mean():.0f} min")
k[2].metric("90th pct wait", f"{view['yard_wait_min'].quantile(0.9):.0f} min")
chilled = view[view["category"] == "perishable"]
k[3].metric("Chilled trucks waiting >45 min", f"{100 * (chilled['yard_wait_min'] > 45).mean():.1f}%")
k[4].metric("Detention cost / day", f"₹{view['detention_rs'].sum() / view['visit_date'].nunique():,.0f}")

# ---- charts
left, right = st.columns(2)
with left:
    st.subheader("Average wait by arrival hour")
    st.line_chart(view.groupby("arrival_hour")["yard_wait_min"].mean().rename("avg wait (min)"))
with right:
    st.subheader("Average wait by truck type")
    st.bar_chart(view.groupby("category")["yard_wait_min"].mean().rename("avg wait (min)"))

st.subheader("Trucks waiting in the yard through one day")
one_day = st.selectbox("Day", sorted(view["visit_date"].unique()))
st.area_chart(trucks_waiting_by_minute(view[view["visit_date"] == one_day]))

left, right = st.columns(2)
with left:
    st.subheader("Dock utilisation (6 am - 10 pm)")
    util = (view.groupby("dock_no")["unload_min"].sum()
            / (view["visit_date"].nunique() * 16 * 60) * 100).round(1)
    st.bar_chart(util.rename("% of working window busy"))
with right:
    st.subheader("Trucks past the 2-hour free time")
    late = view[view["yard_wait_min"] > 120].sort_values("yard_wait_min", ascending=False)
    st.dataframe(late[["truck_id", "vendor_id", "category", "gate_in", "yard_wait_min", "detention_rs"]]
                 .round({"yard_wait_min": 1}), hide_index=True, height=300)

with st.expander("Data quality: what the cleaning step fixed"):
    st.dataframe(report.set_index("policy").T)
