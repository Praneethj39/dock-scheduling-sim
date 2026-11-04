"""
OCR step 1: make fake gate-pass / delivery-challan images.

A real DC gets a paper gate pass (or delivery challan) with every truck.
The guard types it into the system by hand, which is slow and error-prone.
To test an OCR module we need images where we KNOW the right answer, so we
draw our own gate passes with Pillow from the booking data.

What this script does
  1. Picks 200 trucks from the "booked slots" run (the run where suppliers
     book arrival slots, so the booking has a slot time to check against)
  2. Decides what is printed on each pass. Most passes agree with the
     booking, but a few have real problems on purpose:
       - wrong pallet count  (~6%: supplier sent more or fewer pallets)
       - unknown truck       (~5%: a truck that never booked a slot)
       - late / early truck  (~10%, not added by us: these trucks really
                              arrived outside their slot in the simulation)
  3. Draws each pass as a PNG. 25% are made "hard" the way phone photos
     and old scanners are: tilted, blurry, faded, or grainy.
  4. Saves the true printed values to data/ocr/ground_truth.csv so we can
     later score the OCR field by field.

Run:  python ocr_generate.py
"""

import os
import random
import sqlite3

import numpy as np
import pandas as pd
from matplotlib import font_manager
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

SEED = 7
N_PASSES = 200
HARD_SHARE = 0.25
PALLET_ERROR_SHARE = 0.06
UNKNOWN_TRUCK_SHARE = 0.05
DB_PATH = "data/yard.db"
OUT_DIR = "data/ocr"
IMG_DIR = os.path.join(OUT_DIR, "images")
W, H = 1000, 640                       # image size in pixels

# Made-up supplier names, so the pass looks like a real document.
# The OCR only needs the supplier code (V001...), the name is decoration.
SUPPLIER_NAMES = [
    "Konkan Dairy Pvt Ltd", "Shree Ganesh Foods", "Deccan Agro Fresh", "Sahyadri Farms Co",
    "Malabar Spices Ltd", "Godavari Beverages", "Nilgiri Bakers", "Vasant Home Care",
    "Coastal Cold Chain", "Mysore Snacks Co", "Pune Plastics & Homeware", "Indus Detergents",
    "Kaveri Rice Mills", "Ratna Steelware", "Sunrise Packaged Foods", "Green Valley Organics",
    "Annapurna Atta Mills", "Bharat Kitchen Appliances", "Lotus Personal Care", "Narmada Edible Oils",
    "Hubli Furniture Works", "Raigad Fisheries", "Tapti Textiles", "Western Ghats Tea Co",
    "Sona Paper Products", "Mahalaxmi Sweets", "Everfresh Poultry", "Ajanta Electricals",
    "Satara Sugar Works", "Kolhapur Jaggery Co", "Royal Glassware", "Amrut Ice Creams",
    "Vidarbha Pulses", "Golden Grain Exports", "Silverline Cookware", "Prakash Cleaning Supplies",
    "Ocean Frozen Foods", "Tulsi Herbal Care", "Mandovi Mattresses", "Om Sai Batteries",
]
CATEGORY_PRINT = {"perishable": "Chilled", "dry": "Dry / FMCG", "bulky": "Home & Utilities"}


def font(size):
    """DejaVu Sans is a plain office font (close to Verdana), the kind a
    printed challan uses. It comes inside matplotlib, which this project
    already needs, so the images look the same on every computer and the
    accuracy numbers are comparable."""
    return ImageFont.truetype(font_manager.findfont("DejaVu Sans"), size)


def load_bookings():
    with sqlite3.connect(DB_PATH) as con:
        return pd.read_sql_query(
            "SELECT truck_id, vendor_id, category, pallets, slot_start, gate_in "
            "FROM truck_visits WHERE policy = 'slots'", con)


def decide_printed_values(bookings, rng):
    """Choose 200 trucks and decide what each gate pass says."""
    pick = bookings.sample(N_PASSES, random_state=SEED).reset_index(drop=True)
    rows = []
    for i, b in pick.iterrows():
        arrival = pd.Timestamp(b["gate_in"]).floor("min")   # gate pass shows hh:mm
        row = {"pass_id": f"GP{i + 1:04d}", "truck_id": b["truck_id"], "vendor_id": b["vendor_id"],
               "arrival_time": arrival.strftime("%Y-%m-%d %H:%M"), "pallets": int(b["pallets"]),
               "category": b["category"], "planted_issue": "none"}
        r = rng.random()
        if r < UNKNOWN_TRUCK_SHARE:
            # a truck that never booked: id outside the booked range
            row["truck_id"] = f"T{rng.randint(3000, 9999):05d}"
            row["planted_issue"] = "unknown_truck"
        elif r < UNKNOWN_TRUCK_SHARE + PALLET_ERROR_SHARE:
            change = rng.choice([-4, -3, -2, -1, 1, 2, 3])
            row["pallets"] = max(1, row["pallets"] + change)
            row["planted_issue"] = "pallet_count"
        rows.append(row)
    return pd.DataFrame(rows)


def draw_pass(row):
    """Draw one gate pass as a white 'paper' with a header and fields."""
    img = Image.new("RGB", (W, H), (250, 248, 240))           # off-white paper
    d = ImageDraw.Draw(img)
    d.rectangle([20, 20, W - 20, H - 20], outline=(60, 60, 60), width=3)
    d.text((50, 45), "INBOUND GATE PASS / DELIVERY CHALLAN", font=font(34), fill=(20, 20, 20))
    d.text((50, 95), "Retail Distribution Centre - Gate 1", font=font(24), fill=(70, 70, 70))
    d.text((W - 230, 95), f"No. {row['pass_id']}", font=font(24), fill=(70, 70, 70))
    d.line([40, 140, W - 40, 140], fill=(60, 60, 60), width=2)

    vendor_no = int(row["vendor_id"][1:])
    fields = [
        ("Truck No", row["truck_id"]),
        ("Supplier Code", row["vendor_id"]),
        ("Supplier Name", SUPPLIER_NAMES[(vendor_no - 1) % len(SUPPLIER_NAMES)]),
        ("Arrival Time", row["arrival_time"]),
        ("No. of Pallets", str(row["pallets"])),
        ("Goods Type", CATEGORY_PRINT[row["category"]]),
    ]
    y = 170
    for label, value in fields:
        d.text((60, y), f"{label}:", font=font(30), fill=(30, 30, 30))
        d.text((360, y), value, font=font(30), fill=(20, 30, 110))   # filled in blue ink
        y += 58
    d.text((60, H - 80), "Security sign: ____________", font=font(24), fill=(90, 90, 90))
    d.text((W - 400, H - 80), "Received by: ____________", font=font(24), fill=(90, 90, 90))
    return img


def make_hard(img, rng):
    """Apply 2-3 of the problems real photos and scans have. Returns the
    damaged image and a text note of what was done (for the dashboard)."""
    effects = rng.sample(["rotate", "blur", "low_contrast", "noise"], k=rng.choice([2, 3]))
    if "rotate" not in effects and rng.random() < 0.5:
        effects.append("rotate")                 # tilt is the most common real problem
    notes = []
    if "low_contrast" in effects:
        factor = rng.uniform(0.25, 0.4)
        img = ImageEnhance.Contrast(img).enhance(factor)   # faded print / bad light
        img = ImageEnhance.Brightness(img).enhance(rng.uniform(0.9, 1.1))
        notes.append(f"contrast x{factor:.2f}")
    if "blur" in effects:
        radius = rng.uniform(1.3, 2.0)
        img = img.filter(ImageFilter.GaussianBlur(radius))  # out-of-focus photo
        notes.append(f"blur r={radius:.1f}")
    if "noise" in effects:
        arr = np.asarray(img).astype(np.float32)
        sigma = rng.uniform(25, 40)
        np_rng = np.random.default_rng(rng.randint(0, 10**6))
        arr += np_rng.normal(0, sigma, arr.shape[:2])[..., None]   # grainy scan
        img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
        notes.append(f"noise sd={sigma:.0f}")
    if "rotate" in effects:
        angle = rng.choice([-1, 1]) * rng.uniform(3, 10)
        img = img.rotate(angle, expand=True, fillcolor=(255, 255, 255),
                         resample=Image.BICUBIC)               # pass placed crooked
        notes.append(f"rotated {angle:+.1f} deg")
    return img, "; ".join(notes)


def main():
    rng = random.Random(SEED)
    os.makedirs(IMG_DIR, exist_ok=True)
    for old in os.listdir(IMG_DIR):
        os.remove(os.path.join(IMG_DIR, old))

    truth = decide_printed_values(load_bookings(), rng)
    hard_ids = set(rng.sample(list(truth["pass_id"]), int(N_PASSES * HARD_SHARE)))
    truth["difficulty"] = np.where(truth["pass_id"].isin(hard_ids), "hard", "clean")
    truth["damage"] = ""

    for i, row in truth.iterrows():
        img = draw_pass(row)
        if row["difficulty"] == "hard":
            img, note = make_hard(img, rng)
            truth.loc[i, "damage"] = note
        img.save(os.path.join(IMG_DIR, f"{row['pass_id']}.jpg"), quality=90)  # phone photos are JPEGs

    truth.to_csv(os.path.join(OUT_DIR, "ground_truth.csv"), index=False)
    print(f"Saved {len(truth)} gate passes to {IMG_DIR}/ "
          f"({(truth['difficulty'] == 'hard').sum()} hard)")
    print("Planted problems:", truth["planted_issue"].value_counts().to_dict())


if __name__ == "__main__":
    main()
