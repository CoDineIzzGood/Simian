# services/file_scanner.py
import os, hashlib, re

RISKY_EXT = {
    ".exe", ".dll", ".scr", ".bat", ".cmd", ".js", ".vbs", ".ps1", ".jar", ".msi",
    ".com", ".reg"
}

def _sha256(path: str, max_bytes: int = 20_000_000) -> str:
    """Hash the first max_bytes (or whole file if smaller)."""
    h = hashlib.sha256()
    total = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            h.update(chunk)
            if total >= max_bytes:
                break
    return h.hexdigest()

def _has_double_ext(filename: str) -> bool:
    # e.g., "invoice.pdf.exe"
    parts = filename.lower().split(".")
    return len(parts) >= 3 and parts[-1] in {e.strip(".") for e in RISKY_EXT}

def scan_path(path: str) -> str:
    """
    Minimal offline scan. Returns a one-line verdict string.
    - Computes size + sha256 (partial for speed)
    - Flags suspicious extensions/double-extensions
    """
    try:
        if not os.path.isfile(path):
            return f"{path}: not a file"

        size = os.path.getsize(path)
        sha = _sha256(path)
        _, ext = os.path.splitext(path)
        ext = ext.lower()

        verdict = "clean"
        notes = []

        if size > 200_000_000:  # 200 MB
            notes.append("large")
        if ext in RISKY_EXT:
            verdict = "suspicious"
            notes.append(f"risky-ext:{ext}")
        if _has_double_ext(os.path.basename(path)):
            verdict = "suspicious"
            notes.append("double-extension")

        # quick heuristic: lots of base64-ish noise in text files
        try:
            with open(path, "rb") as f:
                head = f.read(4096)
            if re.search(rb"[A-Za-z0-9+/]{200,}={0,2}", head):
                notes.append("base64-like")
        except Exception:
            pass

        tag = f" ({', '.join(notes)})" if notes else ""
        return f"{os.path.basename(path)} — {size} bytes — sha256:{sha[:16]}… — {verdict}{tag}"
    except Exception as e:
        return f"{path}: scan error: {e}"
