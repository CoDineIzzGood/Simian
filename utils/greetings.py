
from datetime import datetime

def get_wakeup_message(name: str | None = None) -> str:
    now = datetime.now()
    hour = now.hour
    if hour < 6:
        base = "Working late?"
    elif hour < 12:
        base = "Good morning."
    elif hour < 18:
        base = "Good afternoon."
    else:
        base = "Good evening."
    if name:
        base += f" {name}"
    return base
