"""Run the whole pipeline in order: simulate -> clean and load -> analyse -> gate-pass OCR.

clean_and_load.py rebuilds data/yard.db from scratch, so the OCR tables are
rebuilt here too. Use --skip-ocr if you don't have Tesseract installed."""

import subprocess
import sys

steps = ["simulate.py", "clean_and_load.py", "analyze.py"]
if "--skip-ocr" not in sys.argv:
    steps.append("run_ocr.py")

for step in steps:
    print(f"\n########## {step} ##########")
    subprocess.run([sys.executable, step], check=True)

print("\nDone. Open the dashboard with:  streamlit run dashboard.py")
