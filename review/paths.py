import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("TCC_DATA_DIR", Path.home() / "Library" / "Application Support" / "truckee-cares"))
DB_PATH = DATA_DIR / "review.sqlite"
KEY_DIR = Path.home() / ".config" / "truckee-cares"
CONFIG = ROOT / "config" / "season.json"
