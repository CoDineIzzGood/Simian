"""
Simian launcher: starts GUI (and optionally the FastAPI backend).

Why this exists:
- Avoids broken uvicorn.exe shebang issues when venv path changes
- Always uses `python -m uvicorn ...` with the current interpreter

Usage:
  python simian_launcher.py
  python simian_launcher.py --no-api
"""
from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import time
from pathlib import Path

API_HOST = "127.0.0.1"
API_PORT = 8000


def port_in_use(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except Exception:
        return False


def start_api(root: Path) -> subprocess.Popen:
    if port_in_use(API_HOST, API_PORT):
        print("[launcher] API already running.")
        return None  # type: ignore
    cmd = [sys.executable, "-m", "uvicorn", "main:app", "--host", API_HOST, "--port", str(API_PORT)]
    print("[launcher] Starting API:", " ".join(cmd))
    return subprocess.Popen(cmd, cwd=str(root))


def start_gui(root: Path) -> int:
    cmd = [sys.executable, "-m", "gui.simian_gui"]
    print("[launcher] Starting GUI:", " ".join(cmd))
    p = subprocess.Popen(cmd, cwd=str(root))
    return p.wait()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-api", action="store_true")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent

    api_proc = None
    if not args.no_api:
        api_proc = start_api(root)
        time.sleep(0.8)

    try:
        code = start_gui(root)
    finally:
        if api_proc and api_proc.poll() is None:
            print("[launcher] Stopping API...")
            api_proc.terminate()

    return int(code or 0)


if __name__ == "__main__":
    raise SystemExit(main())
