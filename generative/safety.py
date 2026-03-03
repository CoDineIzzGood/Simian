from typing import Optional

class SafetyError(Exception):
    pass

def guard_text(text: Optional[str]):
    if not text:
        return
    # expand with your policy later
    banned = ["bioweapon", "explosive recipe", "dox", "credit card dump"]
    t = text.lower()
    if any(k in t for k in banned):
        raise SafetyError("Content not allowed.")
