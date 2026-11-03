"""Run the whole pipeline in order: simulate -> clean and load -> analyse."""

import subprocess
import sys

for step in ["simulate.py", "clean_and_load.py", "analyze.py"]:
    print(f"\n########## {step} ##########")
    subprocess.run([sys.executable, step], check=True)

print("\nDone. Open the dashboard with:  streamlit run dashboard.py")
