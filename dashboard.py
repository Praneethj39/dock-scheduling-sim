"""
Yard monitoring dashboard (Streamlit), reading the cleaned data in data/yard.db.

Two tabs:
  Yard             KPIs, waits, dock utilisation, trucks past free time
  Gate-pass OCR    gate passes read by OCR: approved vs pending, why, accuracy

Run:  streamlit run dashboard.py
Then open the link it prints (usually http://localhost:8501).
"""

import os
import sqlite3

import cv2
import pandas as pd
import streamlit as st

from ocr_preprocess import preprocess

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




@st.cache_data
def load_ocr():
    """OCR tables; None if run_ocr.py hasn't been run yet."""
    with sqlite3.connect(DB_PATH) as con:
        tables = set(pd.read_sql_query("SELECT name FROM sqlite_master", con)["name"])
        if "gate_pass_checks" not in tables:
            return None
        return {name: pd.read_sql_query(f"SELECT * FROM {name}", con)
                for name in ["gate_pass_checks", "ocr_accuracy", "ocr_reads", "ocr_ground_truth"]}


def ocr_tab():
    data = load_ocr()
    if data is None:
        st.info("No OCR results yet. Run `python run_ocr.py` (needs Tesseract installed).")
        return
    checks, acc = data["gate_pass_checks"], data["ocr_accuracy"]

    mode = st.radio("OCR input", ["preprocessed", "raw"], horizontal=True,
                    format_func={"preprocessed": "Cleaned image (OpenCV)", "raw": "Raw photo"}.get,
                    help="Switch to 'Raw photo' to see what the yard would get without preprocessing")
    view = checks[checks["mode"] == mode]
    pending = view[view["status"] != "approved"]
    a_all = acc[(acc["difficulty"] == "all") & (acc["mode"] == mode)].iloc[0]
    a_hard = acc[(acc["difficulty"] == "hard") & (acc["mode"] == mode)].iloc[0]

    k = st.columns(5)
    k[0].metric("Gate passes", f"{len(view)}")
    k[1].metric("Auto-approved", f"{100 * (view['status'] == 'approved').mean():.1f}%")
    k[2].metric("Pending verification", f"{len(pending)}")
    k[3].metric("Field accuracy, all", f"{a_all['field_avg']:.1f}%")
    k[4].metric("Field accuracy, hard images", f"{a_hard['field_avg']:.1f}%")

    left, right = st.columns(2)
    with left:
        st.subheader("Why passes are pending")
        reasons = pending["reasons"].str.split("; ").explode().value_counts()
        st.bar_chart(reasons.rename("gate passes"), horizontal=True)
        st.caption("A pass can have more than one reason.")
    with right:
        st.subheader("OCR accuracy: raw vs cleaned image")
        chart = (acc[acc["difficulty"] != "all"]
                 .pivot(index="difficulty", columns="mode", values="field_avg")[["raw", "preprocessed"]])
        st.bar_chart(chart, stack=False, y_label="% of fields exactly right")
        st.dataframe(acc.set_index(["difficulty", "mode"])
                     [["truck_id", "vendor_id", "arrival_time", "pallets", "field_avg", "all_4_correct"]])

    st.subheader("Pending verification queue")
    cols = ["pass_id", "reasons", "truck_id", "booked_truck", "vendor_id", "booked_vendor",
            "pallets", "booked_pallets", "arrival_time", "slot_start"]
    st.dataframe(pending[cols], hide_index=True, height=280)

    with st.expander("Checking the checker (possible only because the passes are synthetic)"):
        st.write("Every pass's real story is known, so we can see whether the rules flagged "
                 "the right ones. A 'false alarm' is a correct pass the OCR misread; the guard "
                 "checks it by hand, which costs time but is safe. 'Missed' is the dangerous one.")
        st.dataframe(view["outcome"].value_counts().rename("gate passes"))

    st.subheader("Look at one gate pass")
    pick = st.selectbox("Gate pass", view["pass_id"], index=int(view["pass_id"].isin(pending["pass_id"]).argmax()))
    row = view[view["pass_id"] == pick].iloc[0]
    truth = data["ocr_ground_truth"].set_index("pass_id").loc[pick]
    path = f"data/ocr/images/{pick}.jpg"
    c1, c2 = st.columns(2)
    if os.path.exists(path):
        damage = f": {truth['damage']}" if pd.notna(truth["damage"]) and truth["damage"] else ""
        c1.image(path, caption=f"Photo ({truth['difficulty']}{damage})")
        c2.image(preprocess(cv2.imread(path))[0], caption="After OpenCV cleaning")
    else:
        c1.info("Images not found. Run `python ocr_generate.py` to recreate them.")

    def show(v):                                   # 11.0 -> "11", None -> "-"
        if pd.isna(v):
            return "-"
        return str(int(v)) if isinstance(v, float) else str(v)

    c1, c2 = st.columns([3, 2])
    c1.markdown(f"**Status:** {row['status']}  \n**Reasons:** {row['reasons'] or '-'}")
    c1.dataframe(pd.DataFrame({
        "OCR read": [row["truck_id"], row["vendor_id"], row["arrival_time"], row["pallets"]],
        "Booking": [row["booked_truck"], row["booked_vendor"], row["slot_start"], row["booked_pallets"]],
        "Actually printed": [truth["truck_id"], truth["vendor_id"], truth["arrival_time"], truth["pallets"]],
    }, index=["truck", "supplier", "arrival (booking: slot start)", "pallets"]).map(show))
    raw_text = data["ocr_reads"].query("pass_id == @pick and mode == @mode")["ocr_text"].iloc[0]
    with c2.expander("Raw OCR text", expanded=True):
        st.text(raw_text)


st.set_page_config(page_title="Yard dashboard", layout="wide")
st.title("Inbound yard dashboard")
tab_yard, tab_ocr = st.tabs(["Yard", "Gate-pass OCR"])

with tab_ocr:
    ocr_tab()

with tab_yard:
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
