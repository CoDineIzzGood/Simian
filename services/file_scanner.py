import hashlib
from pathlib import Path
from typing import List, Dict

def sha1_file(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def scan_uploads(root: str = "data/uploads") -> List[Dict]:
    p = Path(root)
    p.mkdir(parents=True, exist_ok=True)
    out = []
    for fp in p.rglob("*"):
        if fp.is_file():
            st = fp.stat()
            out.append({
                "path": str(fp),
                "size": st.st_size,
                "mtime": int(st.st_mtime),
                "sha1": sha1_file(fp),
            })
    return out
