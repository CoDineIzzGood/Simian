# services/simian.py
from __future__ import annotations
import datetime

SIMIAN_NAME = "Simian"

def time_of_day_greeting()->str:
    h = datetime.datetime.now().hour
    if 4 <= h < 12:   return "Good morning."
    if 12 <= h < 17:  return "Good afternoon."
    if 17 <= h < 22:  return "Good evening."
    return "Working late?"

SYSTEM_PERSONA = (
    f"You are {SIMIAN_NAME}, a helpful desktop AI assistant. "
    "Stay concise, friendly, and avoid calling yourself 'llama' or a 'generic model'. "
    "Use first‑person singular. Keep answers tight unless asked for details."
)
