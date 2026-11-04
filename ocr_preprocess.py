"""
OCR step 2a: clean up a gate-pass photo before OCR reads it.

Tesseract (the OCR engine) was built for straight, sharp, dark-on-light
pages. A phone photo of a gate pass is often tilted, blurry, faded or
grainy, and Tesseract then misreads characters or skips whole lines.
So we fix the image first, in this order:

  1. Grayscale   colour adds nothing for reading text; one channel is simpler
  2. Denoise     ONLY if the image is grainy. A median filter removes grain,
                 but on a clean image it also erases thin marks like '-'
                 and ':', which broke every date. So we measure first.
  3. Stretch     make the darkest ink black and the paper white, so a faded
                 print gets its contrast back
  4. Threshold   turn every pixel pure black or white (Otsu's method picks
                 the cut-off automatically). We use this black/white copy
                 to measure the tilt.
  5. Deskew      find how much the page is tilted and rotate it straight

The image we hand to Tesseract is the straightened GRAYSCALE page, not the
black/white one. We tested both (see the README): Tesseract has its own
thresholding that works better on blurry letters than one global Otsu
cut-off, which turned blurred strokes into broken blobs.

Run this file directly to save a before/after picture for the README:
    python ocr_preprocess.py
"""

import cv2
import numpy as np

MAX_SKEW_DEG = 15        # we look for tilts between -15 and +15 degrees
NOISE_LIMIT = 2.0        # grain level above which we denoise (clean pages measure 0, grainy 4-20)


def to_gray(img_bgr):
    return cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)


def noise_level(gray):
    """How grainy is the image? Compare each pixel with the median of its
    3x3 neighbourhood. On clean paper they are almost equal; on a grainy
    photo they differ a lot. We return the typical (median) difference."""
    diff = cv2.absdiff(gray, cv2.medianBlur(gray, 3))
    return float(np.median(diff))


def denoise(gray):
    # Median of each 5x5 patch: a lone noisy dot is outvoted by its neighbours.
    # A light Gaussian blur afterwards smooths what is left.
    return cv2.GaussianBlur(cv2.medianBlur(gray, 5), (3, 3), 0)


def stretch_contrast(gray):
    """Map the darkest ink to black and the paper to white. Pure-white pixels
    (>= 250) are the empty corners added when a tilted photo is rotated, not
    paper, so we leave them out when measuring; otherwise a faded page looks
    'already bright enough' and nothing changes."""
    page = gray[gray < 250]
    if page.size == 0:
        return gray
    lo, hi = np.percentile(page, (1, 99))      # ignore the 1% extremes (noise)
    out = (gray.astype(np.float32) - lo) / max(hi - lo, 1) * 255
    return np.clip(out, 0, 255).astype(np.uint8)


def threshold(gray):
    # A light blur first so Otsu sees "text" vs "paper", not individual grains
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    _, binary = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return binary                        # text = 0 (black), paper = 255 (white)


def find_skew_angle(binary):
    """Projection-profile method. Rotate the page by each candidate angle
    and count the black pixels in every row. When the page is straight,
    rows of text and the blank gaps between them alternate sharply, so the
    row counts vary the most. The angle with the biggest variation wins."""
    small = cv2.resize(255 - binary, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
    h, w = small.shape
    centre = (w / 2, h / 2)

    def score(angle):
        m = cv2.getRotationMatrix2D(centre, angle, 1.0)
        rotated = cv2.warpAffine(small, m, (w, h), flags=cv2.INTER_NEAREST, borderValue=0)
        return np.var(rotated.sum(axis=1, dtype=np.float64))

    coarse = max(np.arange(-MAX_SKEW_DEG, MAX_SKEW_DEG + 0.1, 1.0), key=score)
    fine = max(np.arange(coarse - 1, coarse + 1.01, 0.1), key=score)  # zoom in
    return float(fine)


def rotate(gray, angle):
    h, w = gray.shape
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(gray, m, (w, h), flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=255)


def preprocess(img_bgr):
    """Full pipeline. Returns the cleaned image plus what it did."""
    gray = to_gray(img_bgr)
    noise = noise_level(gray)
    if noise > NOISE_LIMIT:
        gray = denoise(gray)
    gray = stretch_contrast(gray)
    angle = find_skew_angle(threshold(gray))
    if abs(angle) >= 0.3:                # don't resample a page that is already straight
        gray = rotate(gray, angle)
    return gray, {"deskew_deg": round(angle, 1), "noise_level": round(noise, 1),
                  "denoised": noise > NOISE_LIMIT}


if __name__ == "__main__":
    # Save a side-by-side "before / after" for one hard gate pass
    import pandas as pd

    truth = pd.read_csv("data/ocr/ground_truth.csv")
    example = truth[(truth["difficulty"] == "hard")
                    & truth["damage"].str.contains("rotated")
                    & truth["damage"].str.contains("noise")].iloc[0]
    before = cv2.imread(f"data/ocr/images/{example['pass_id']}.jpg")
    after, info = preprocess(before)
    after_bgr = cv2.cvtColor(after, cv2.COLOR_GRAY2BGR)
    h = 500
    panel = np.hstack([cv2.resize(before, (int(before.shape[1] * h / before.shape[0]), h)),
                       np.full((h, 20, 3), 255, np.uint8),
                       cv2.resize(after_bgr, (int(after.shape[1] * h / after.shape[0]), h))])
    cv2.imwrite("results/ocr_before_after.png", panel)
    print(f"{example['pass_id']} ({example['damage']}): {info} -> results/ocr_before_after.png")
