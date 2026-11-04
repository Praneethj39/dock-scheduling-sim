"""Run the gate-pass OCR module in order (needs data/yard.db, so run the
main pipeline first): make images -> OCR them -> validate -> score.
Takes about 1-2 minutes, almost all of it Tesseract reading 400 images."""

import subprocess
import sys

for step in ["ocr_generate.py", "ocr_preprocess.py", "ocr_read.py", "ocr_validate.py", "ocr_accuracy.py"]:
    print(f"\n########## {step} ##########")
    subprocess.run([sys.executable, step], check=True)

print("\nDone. See the 'Gate-pass OCR' tab in:  streamlit run dashboard.py")
