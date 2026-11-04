"""
OCR steps 2b + 3: read every gate pass with Tesseract, pull out the four
fields with regular expressions, and save them to SQLite.

Each image is read TWICE:
  raw           the image goes straight to Tesseract (the "before")
  preprocessed  the image is cleaned by ocr_preprocess.py first (the "after")
Reading both ways is what lets us measure how much the cleaning helped.

Fields we extract
  truck_id      T + 5 digits      e.g. T01426
  vendor_id     V + 3 digits      e.g. V026   (supplier code)
  arrival_time  YYYY-MM-DD HH:MM  e.g. 2026-10-21 13:10
  pallets       a number          e.g. 20

Output table in data/yard.db: ocr_reads (one row per image per mode)

Needs the Tesseract program installed (pytesseract only talks to it):
  Mac: brew install tesseract   |   Ubuntu: sudo apt install tesseract-ocr
  Windows: installer from https://github.com/UB-Mannheim/tesseract/wiki

Run:  python ocr_read.py      (about 1-2 minutes for 400 reads)
"""

import os
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import cv2
import pandas as pd
import pytesseract

from ocr_preprocess import preprocess

DB_PATH = "data/yard.db"
OCR_DIR = "data/ocr"
# --psm 6 = "treat the image as one block of text", which suits a form
# laid out as lines of "Label: value". Same setting for both modes, so the
# only difference between raw and preprocessed is the image cleaning.
TESS_CONFIG = "--oem 3 --psm 6"

# Letters OCR often confuses with digits. Only used inside fields that
# must be numbers, so "S" in a supplier NAME is never touched.
DIGIT_FIX = str.maketrans({"O": "0", "o": "0", "D": "0", "Q": "0",
                           "I": "1", "l": "1", "i": "1", "|": "1", "!": "1",
                           "Z": "2", "z": "2", "S": "5", "s": "5",
                           "B": "8", "G": "6", "g": "9"})
DIGITISH = r"[0-9OoDQIli|!ZzSsBGg]"


def fix_digits(text):
    return text.translate(DIGIT_FIX)


def find_line(lines, label_pattern):
    """Return the text AFTER a label like 'Truck No:' on the first line that
    has it. Labels are matched loosely because OCR may read 'Truck N0'."""
    for line in lines:
        m = re.search(label_pattern + r"\s*[:;.,]?\s*(.*)", line, flags=re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def extract_fields(text):
    """Turn the OCR text into the four fields. A field we can't find is None."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    out = {"truck_id": None, "vendor_id": None, "arrival_time": None, "pallets": None}

    # IDs have a fixed length. Tesseract sometimes repeats a character
    # ("TO00976", "Vvo10"), so we take the run of digit-like characters after
    # the prefix letter and keep only the LAST 5 (truck) or 3 (supplier).

    # Truck: after "Truck No" (T is sometimes read as 7), else T + digits anywhere
    after = find_line(lines, r"Tr[uv]ck\s*N[o0]")
    m = re.search(rf"[T7]\s?({DIGITISH}{{5,7}})\b", after or "") or \
        re.search(rf"\bT\s?({DIGITISH}{{5,7}})\b", text)
    if m:
        out["truck_id"] = "T" + fix_digits(m.group(1))[-5:]

    # Supplier code: after "Supplier Code" (V is sometimes read as v, Vv or Y)
    after = find_line(lines, r"Supp\w*\s*C[o0]de")
    m = re.search(rf"[VvYy]{{1,2}}\s?({DIGITISH}{{3,5}})\b", after or "") or \
        re.search(rf"\b[Vv]{{1,2}}\s?({DIGITISH}{{3,5}})\b", text)
    if m:
        out["vendor_id"] = "V" + fix_digits(m.group(1))[-3:]

    # Arrival time: the only date-time on the pass. A '-' can be misread as
    # '.' or ',' or lost completely, so between the numbers we accept any of
    # those or just a space, then rebuild a clean value.
    sep = r"(?:\s*[-.,~]\s*|\s+)"
    m = re.search(rf"({DIGITISH}{{4}}){sep}({DIGITISH}{{2}}){sep}({DIGITISH}{{2}})"
                  rf"\s+({DIGITISH}{{1,2}})\s*[:.;,]?\s*({DIGITISH}{{2}})\b", text)
    if m:
        y, mo, d, hh, mm = (fix_digits(g) for g in m.groups())
        stamp = pd.to_datetime(f"{y}-{mo}-{d} {hh}:{mm}", format="%Y-%m-%d %H:%M", errors="coerce")
        if pd.notna(stamp):                  # reject impossible dates like month 13
            out["arrival_time"] = stamp.strftime("%Y-%m-%d %H:%M")

    # Pallets: the first 1-3 digit-like characters after "Pallets"
    after = find_line(lines, r"Pa[l1I|]{1,2}e[tf]s?")
    m = re.search(rf"({DIGITISH}{{1,3}})\b", after or "")
    if m:
        out["pallets"] = int(fix_digits(m.group(1)))
    return out


def read_one(pass_id):
    """OCR one image both ways. Returns two result rows."""
    img = cv2.imread(os.path.join(OCR_DIR, "images", f"{pass_id}.jpg"))
    rows = []
    for mode in ["raw", "preprocessed"]:
        start = time.perf_counter()
        info = {"deskew_deg": 0.0, "noise_level": None, "denoised": False}
        if mode == "preprocessed":
            ocr_input, info = preprocess(img)
        else:
            ocr_input = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)   # Tesseract expects RGB
        text = pytesseract.image_to_string(ocr_input, config=TESS_CONFIG)
        rows.append({"pass_id": pass_id, "mode": mode, **extract_fields(text), **info,
                     "ocr_text": text,
                     "seconds": round(time.perf_counter() - start, 2)})
    return rows


def main():
    truth = pd.read_csv(os.path.join(OCR_DIR, "ground_truth.csv"))
    start = time.perf_counter()
    # Tesseract runs as a separate program, so several images can be read
    # at the same time on a multi-core laptop
    with ThreadPoolExecutor(max_workers=os.cpu_count()) as pool:
        results = [row for rows in pool.map(read_one, truth["pass_id"]) for row in rows]
    reads = pd.DataFrame(results)
    reads["pallets"] = reads["pallets"].astype("Int64")

    with sqlite3.connect(DB_PATH) as con:
        reads.to_sql("ocr_reads", con, index=False, if_exists="replace")
        truth.to_sql("ocr_ground_truth", con, index=False, if_exists="replace")

    found = reads.groupby("mode")[["truck_id", "vendor_id", "arrival_time", "pallets"]].count()
    print(f"Read {len(truth)} gate passes x 2 modes in {time.perf_counter() - start:.0f} s")
    print("Fields found (out of", len(truth), ")\n", found.to_string())


if __name__ == "__main__":
    main()
