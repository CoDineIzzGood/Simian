import os
import traceback
from datetime import datetime

CRASH_LOG_FILE = "logs/simian_crashes.log"

def log_crash(module: str, error: Exception):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    crash_info = f"\n--- CRASH @ {timestamp} ---\nModule: {module}\n{traceback.format_exc()}\n"

    os.makedirs(os.path.dirname(CRASH_LOG_FILE), exist_ok=True)
    with open(CRASH_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(crash_info)
