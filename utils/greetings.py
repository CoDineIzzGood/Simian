# utils/greetings.py
from __future__ import annotations
from datetime import datetime

def get_wakeup_message() -> str:
    h = datetime.now().hour
    if 5 <= h < 12:
        return "Good morning."
    if 12 <= h < 18:
        return "Good afternoon."
    if 18 <= h < 22:
        return "Good evening."
    return "Working late?"
