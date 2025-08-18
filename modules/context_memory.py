
_context_store = []

def save_context(text):
    _context_store.append(text)
    print(f"[Context] Saved: {text}")

def load_context():
    return _context_store
