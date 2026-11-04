"""
OCR step 5: how accurate is the OCR, field by field?

We drew the gate passes ourselves, so we know exactly what is printed on
each one (data/ocr/ground_truth.csv). A field counts as correct only if
the OCR value matches the printed value EXACTLY. "T01426" vs "T01428" is
wrong, the same way the yard system would treat it.

We score four ways: clean vs hard images x raw vs preprocessed. The gap
between raw and preprocessed is what the image cleaning bought us.

We also count:
  all-4-correct  share of passes where every field is right (one wrong
                 field still means a person has to look at the pass)
  not found      the regex found nothing (vs found something but wrong)

Output: table ocr_accuracy in data/yard.db and results/ocr_accuracy.csv

Optional: python ocr_accuracy.py --ablation
  Re-reads the 50 hard images, adding one cleaning step at a time, to show
  what each step adds (about 1 minute). Saved to results/ocr_ablation.csv

Run:  python ocr_accuracy.py
"""

import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor

import cv2
import pandas as pd
import pytesseract

import ocr_preprocess as P
from ocr_read import TESS_CONFIG, extract_fields

DB_PATH = "data/yard.db"
FIELDS = ["truck_id", "vendor_id", "arrival_time", "pallets"]


def load_scored():
    """One row per image per mode, with a True/False column per field."""
    with sqlite3.connect(DB_PATH) as con:
        reads = pd.read_sql_query("SELECT * FROM ocr_reads", con)
        truth = pd.read_sql_query("SELECT * FROM ocr_ground_truth", con)
    df = reads.merge(truth[["pass_id", "difficulty", "damage"] + FIELDS],
                     on="pass_id", suffixes=("", "_true"))
    # pallets come back from SQLite as 20.0 when some are missing; compare as whole numbers
    df["pallets"] = pd.to_numeric(df["pallets"]).astype("Int64")
    for f in FIELDS:
        df[f + "_ok"] = (df[f].astype("string") == df[f + "_true"].astype("string")).fillna(False)
        df[f + "_missing"] = df[f].isna()
    df["all_ok"] = df[[f + "_ok" for f in FIELDS]].all(axis=1)
    return df


def summarise(df):
    rows = []
    groups = list(df.groupby(["difficulty", "mode"])) + \
             [(("all", m), g) for m, g in df.groupby("mode")]
    for (difficulty, mode), g in groups:
        row = {"difficulty": difficulty, "mode": mode, "passes": len(g)}
        for f in FIELDS:
            row[f] = round(100 * g[f + "_ok"].mean(), 1)
        row["field_avg"] = round(100 * g[[f + "_ok" for f in FIELDS]].to_numpy().mean(), 1)
        row["all_4_correct"] = round(100 * g["all_ok"].mean(), 1)
        row["fields_not_found"] = int(g[[f + "_missing" for f in FIELDS]].to_numpy().sum())
        rows.append(row)
    return pd.DataFrame(rows)


def by_damage(df):
    """Which kind of damage hurts most? (hard images only; most have 2-3 kinds)"""
    hard = df[df["difficulty"] == "hard"]
    rows = []
    for effect in ["rotated", "blur", "contrast", "noise"]:
        has = hard[hard["damage"].str.contains(effect, na=False)]
        for mode, g in has.groupby("mode"):
            rows.append({"damage": effect, "mode": mode, "passes": len(g),
                         "field_avg": round(100 * g[[f + "_ok" for f in FIELDS]].to_numpy().mean(), 1)})
    return pd.DataFrame(rows).pivot(index=["damage", "passes"], columns="mode", values="field_avg")


def ablation():
    """Add the cleaning steps one by one and score each version on the hard images."""
    def gray_only(img):
        return P.to_gray(img)

    def plus_denoise_stretch(img):
        g = P.to_gray(img)
        if P.noise_level(g) > P.NOISE_LIMIT:
            g = P.denoise(g)
        return P.stretch_contrast(g)

    def plus_deskew(img):                       # = the final pipeline
        return P.preprocess(img)[0]

    def final_but_black_white(img):             # what if we gave Tesseract our Otsu image?
        return P.threshold(P.preprocess(img)[0])

    steps = [("1. raw colour image", lambda img: cv2.cvtColor(img, cv2.COLOR_BGR2RGB)),
             ("2. + grayscale", gray_only),
             ("3. + denoise (if grainy) + contrast stretch", plus_denoise_stretch),
             ("4. + deskew  (final pipeline)", plus_deskew),
             ("5. final, but sent as Otsu black/white", final_but_black_white)]
    truth = pd.read_csv("data/ocr/ground_truth.csv")
    hard = truth[truth["difficulty"] == "hard"]
    rows = []
    for name, fn in steps:
        def score(row):
            img = cv2.imread(f"data/ocr/images/{row.pass_id}.jpg")
            got = extract_fields(pytesseract.image_to_string(fn(img), config=TESS_CONFIG))
            return [str(got[f]) == str(getattr(row, f)) for f in FIELDS]
        with ThreadPoolExecutor() as pool:
            ok = pd.DataFrame(list(pool.map(score, hard.itertuples())), columns=FIELDS)
        rows.append({"pipeline": name, **(ok.mean() * 100).round(1).to_dict(),
                     "field_avg": round(100 * ok.to_numpy().mean(), 1)})
    out = pd.DataFrame(rows)
    out.to_csv("results/ocr_ablation.csv", index=False)
    print("Hard images: field accuracy as each cleaning step is added")
    print(out.to_string(index=False))


def main():
    if "--ablation" in sys.argv:
        ablation()
        return
    df = load_scored()
    acc = summarise(df)
    with sqlite3.connect(DB_PATH) as con:
        acc.to_sql("ocr_accuracy", con, index=False, if_exists="replace")
    acc.to_csv("results/ocr_accuracy.csv", index=False)
    dmg = by_damage(df)
    dmg.to_csv("results/ocr_accuracy_by_damage.csv")

    print("Field-level OCR accuracy (% of passes where the field is exactly right)")
    print(acc.to_string(index=False))
    print("\nHard images, average field accuracy by kind of damage")
    print(dmg.to_string())


if __name__ == "__main__":
    main()
