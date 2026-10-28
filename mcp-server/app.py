"""Stable first-version entry inside the original D-drive workspace."""
from pathlib import Path
import runpy
if __name__=='__main__':
    runpy.run_path(str(Path(__file__).resolve().parent/'program_v1/entry.py'),run_name='__main__')
