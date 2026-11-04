# Gate-pass OCR: step-by-step walkthrough

This is the "learn it" version of the OCR module. The README has the
results; this file explains each step in plain words, what to look at in
the code, and what to say if an interviewer asks **"why did you do this?"**

Rough time plan (about 4-5 hours if you read the code and run each step):

| Step | Time | File |
|---|---|---|
| 0. Install Tesseract, run once end to end | 20 min | `run_ocr.py` |
| 1. Make the gate passes | 40 min | `ocr_generate.py` |
| 2. Clean the images | 60 min | `ocr_preprocess.py` |
| 3. OCR + regex into SQLite | 50 min | `ocr_read.py` |
| 4. Validate against bookings | 40 min | `ocr_validate.py` |
| 5. Measure accuracy | 30 min | `ocr_accuracy.py` |
| 6. Dashboard tab | 30 min | `dashboard.py` (`ocr_tab`) |
| 7. Re-read the README section | 20 min | `README.md` |

---

## Step 0: set up

```bash
brew install tesseract          # Mac. Ubuntu: sudo apt install tesseract-ocr
pip3 install -r requirements.txt
python3 run_all.py              # or, if yard.db already exists: python3 run_ocr.py
```

`pytesseract` is only a messenger: it saves your image, runs the
`tesseract` program on it, and reads back the text. If you see
`TesseractNotFoundError`, the program isn't installed or isn't on PATH.

Your numbers may differ by a point or two from the README if your
Tesseract version is different (these came from 5.3.4).

---

## Step 1: make the gate passes (`ocr_generate.py`)

**What it does, in plain words**

1. Takes the **booked slots** run from `truck_visits` (that run has a
   slot time for every truck, so "arrived outside slot" can be checked).
2. Picks 200 trucks at random (fixed seed, so it's the same 200 every time).
3. Decides what's printed. Most passes match the booking exactly. A few
   get a planted problem: pallet count changed by 1-4 (~6%) or a truck
   number that never booked (~5%). The "arrived outside slot" cases are
   not planted; those trucks really came early or late in the simulation.
4. Draws each pass with Pillow (`ImageDraw.text`) in DejaVu Sans, a normal
   office font that ships with matplotlib, so it looks the same on any computer.
5. For 50 passes (25%), applies 2-3 kinds of damage: rotate 3-10 deg,
   Gaussian blur, contrast cut to 25-40%, Gaussian noise.
6. Saves JPEGs (phone photos are JPEGs) and `ground_truth.csv`.

**Look at:** `decide_printed_values()` and `make_hard()`. Open a few
images in `data/ocr/images/` and compare a clean one with a hard one.

**Why did you do this?**
> "To measure OCR accuracy I need to know the correct value of every
> field, and real gate passes don't come with an answer key. Generating
> them from my booking data gives me exact ground truth. I also planted
> some real discrepancies, like wrong pallet counts and unbooked trucks,
> so I could test whether my validation rules catch actual problems and
> not just OCR mistakes. I made a quarter of them hard, because a guard's
> phone photo is tilted, blurry or badly lit, and a model that only works
> on perfect scans isn't useful."

**Why JPEG and not PNG?** Phone cameras save JPEG, and JPEG adds its own
small blocky artefacts, so it's more realistic. It was also 21 MB vs 52 MB.

---

## Step 2: clean the images (`ocr_preprocess.py`)

**What each step does**

| Step | OpenCV call | In plain words |
|---|---|---|
| Grayscale | `cv2.cvtColor(..., COLOR_BGR2GRAY)` | 3 colour channels -> 1 brightness channel |
| Measure grain | `noise_level()` | Typical difference between a pixel and the median of its 3x3 neighbours. Clean pages measure 0; grainy ones 4-20 |
| Denoise (only if grainy) | `cv2.medianBlur(5)` + light `GaussianBlur` | Each pixel becomes the median of its neighbours, so isolated dots disappear |
| Contrast stretch | `stretch_contrast()` | The darkest 1% becomes black, the lightest 1% white; a faded page gets its contrast back |
| Otsu threshold | `cv2.threshold(..., THRESH_OTSU)` | Every pixel becomes pure black or white; Otsu picks the cut-off automatically |
| Deskew | `find_skew_angle()` + `cv2.warpAffine` | Try angles from -15 to +15 deg; the right one makes text rows line up, so the black-pixel count per row is most "spiky" (highest variance). Then rotate back |

Run `python3 ocr_preprocess.py` to make `results/ocr_before_after.png`.

**Two things I learned by testing (good interview stories)**

1. **Denoise only when needed.** First I median-filtered every image. On
   clean passes it erased the thin '-' and ':' in dates, and clean-image
   arrival-time accuracy went to 0%. So the code measures grain first.
2. **Threshold for measuring, not for reading.** Feeding Tesseract my Otsu
   black/white image scored 70.5% on hard images; feeding it the cleaned,
   straightened *grayscale* image scored 74.5%. Otsu uses one cut-off for
   the whole page, and on blurred letters it snaps strokes apart.
   Tesseract has its own local thresholding that handles that better. I
   still use Otsu, because the deskew step needs a black/white image.

**Why did you do this?**
> "Tesseract expects straight, sharp, dark-on-light text. On my hard
> images it got only 43.5% of fields right. Each preprocessing step fixes
> one specific problem: denoising for grain, contrast stretching for faded
> prints, deskewing for tilt. I added them one at a time and measured each:
> denoise plus contrast took hard images from 43 to 65%, and deskew took
> them to 74.5%. I also tried things that didn't work, like denoising
> everything and feeding Tesseract a binarised image, and dropped them
> because the numbers went down."

**Why projection-profile deskew and not `minAreaRect`?** `minAreaRect`
fits one box around all the dark pixels; the page border, noise dots and
rotation corners throw it off. The projection method only cares whether
text rows line up, so it's robust here. It found the true tilt to within
0.1 deg on all 41 tilted passes.

---

## Step 3: OCR + regex into SQLite (`ocr_read.py`)

**What it does**

1. Reads each image twice: raw, and after `preprocess()`. Same Tesseract
   setting (`--psm 6`, "one block of text") both times, so the only
   difference is the cleaning.
2. `extract_fields()` turns the text into 4 fields:
   - Finds the **label** first ("Truck No", "Supplier Code", "Pallets"),
     matched loosely because OCR may write "Truck N0" or "Paiiets".
   - Takes what comes after it and matches the expected **format**:
     T + 5 digits, V + 3 digits, YYYY-MM-DD HH:MM, a 1-3 digit number.
   - Inside number fields only, fixes common letter/digit swaps:
     O->0, I/l->1, S->5, B->8 ...
   - IDs have a fixed length, so if Tesseract repeats a character
     ("TO00976", "Vvo10"), it keeps the last 5 / 3 digits.
   - Dates are checked with `pd.to_datetime`, so "2026-13-45" is rejected.
3. Saves one row per image per mode to table `ocr_reads`, including the
   raw OCR text (useful when you need to see *why* a field failed).
4. Uses a thread pool, because Tesseract runs as a separate program and
   can read 2+ images at once (86 s for 400 reads on 2 cores).

**Why did you do this?**
> "Tesseract gives me a block of text, not fields, so I need to find each
> value. I anchor on the label and then match the exact format each field
> must have, because a truck number is always T plus five digits. Format
> rules let me fix predictable OCR confusions, like the letter O for a
> zero, but only inside number fields, so supplier names aren't touched.
> I stored the results in SQLite next to the yard data so validation can
> be a SQL join."

**Why regex and not a machine-learning extractor?** The layout is fixed and
the formats are strict, so regex is accurate, fast and easy to explain. With
200 documents there isn't enough data to train anything. For many different
supplier layouts I'd switch to a layout-aware model.

---

## Step 4: validate against the booking (`ocr_validate.py`)

**What it does**

1. A SQL `LEFT JOIN` of `ocr_reads` to the bookings (`truck_visits`,
   slots run) on `truck_id`. LEFT JOIN keeps passes whose truck isn't
   booked, with empty booking columns.
2. Rules, in order: unreadable field -> unknown truck -> supplier mismatch
   -> pallet count mismatch -> arrived outside slot (more than 15 min
   before the 30-min slot or 15 min after it).
3. No reasons -> **approved**. Any reason -> **pending verification**, with
   all reasons listed.
4. Because the data is synthetic, it also labels each pass as: correctly
   approved, real problem caught, false alarm (OCR misread), or MISSED.

**Why did you do this?**
> "OCR is never 100% right, so I never let it update the system on its
> own. I compare every read with what the supplier booked. If all four
> fields match, two independent sources agree and it's safe to
> auto-approve. If anything differs, a guard checks just that pass. The
> reason tells them what to look at. The result: 71.5% of passes go
> straight through, every one of the 40 real problems was flagged, and
> no wrong pass was approved. The cost is 17 false alarms, which are
> manual checks, not errors."

**Why is a false alarm acceptable but a miss isn't?** A false alarm costs the guard
a quick manual check. A miss means wrong data in the system: the DC
pays for pallets it didn't get, or an unbooked truck takes a dock.

---

## Step 5: measure accuracy (`ocr_accuracy.py`)

**What it does**

- Compares each OCR field with `ground_truth.csv`. A field is correct only
  if it matches **exactly**.
- Reports per field, the average, and "all 4 correct", for clean/hard x
  raw/preprocessed. Also by damage type.
- `python3 ocr_accuracy.py --ablation` re-reads the hard images adding one
  cleaning step at a time.

**Why did you do this?**
> "I measured field-level accuracy, not character accuracy, because the
> business only cares whether the truck number is right, and one wrong
> digit means a wrong truck. I split clean and hard images, because a
> single average hides that the hard ones were at 43%. And I measured
> before and after preprocessing on the same images, so the improvement
> comes only from the cleaning."

**Honesty point to mention yourself:** I tuned the preprocessing while
looking at these same 200 images. On a fresh set of 200 that I never looked
at, hard images scored 69.5% instead of 74.5%. Saying this shows you
understand overfitting.

---

## Step 6: dashboard tab (`dashboard.py`, function `ocr_tab`)

- KPI tiles: passes, % auto-approved, pending count, field accuracy (all, hard)
- Bar chart of pending reasons, and raw vs preprocessed accuracy
- The pending queue: OCR value next to booked value for every field
- A switch between "Cleaned image" and "Raw photo", to show the difference live
- A viewer: pick any pass and see the photo, the cleaned image, OCR vs
  booking vs what was actually printed, and the raw OCR text

**Why did you do this?**
> "The guard needs a work queue, not a report: which passes to check and
> what's wrong with each. The manager needs to know if the OCR is good
> enough, so accuracy sits next to it. The raw-vs-cleaned switch lets me
> show the effect of preprocessing in a demo in five seconds."

---

## Your numbers (run on 4 Nov 2026, Tesseract 5.3.4)

| | Raw photo | After preprocessing |
|---|---|---|
| Field accuracy, clean images | 94.2% | **99.2%** |
| Field accuracy, hard images | 43.5% | **74.5%** |
| Field accuracy, all 200 | 81.5% | **93.0%** |
| All 4 fields right | 66.5% | **88.5%** |
| Auto-approved | 50.5% | **71.5%** |
| False alarms | 59 | **17** |
| Real problems missed | 0 | **0** |

Main mismatch types (preprocessed): arrived outside slot 18, pallet count
mismatch 17, unreadable field 15, supplier mismatch 6, unknown truck 4.

A resume line that matches these numbers:

> Built an OCR gate-pass capture module (OpenCV, Tesseract, regex, SQLite):
> preprocessing raised field accuracy on degraded images from 44% to 75%
> (93% overall); validation against supplier bookings auto-approved 72% of
> passes and flagged all 40 real discrepancies with no false approvals.
