from pathlib import Path
import subprocess, shlex, os, uuid, asyncio
from .config import SANDBOX_DIR

PY = os.getenv("PYTHON_EXECUTABLE", "python")

SANDBOX_TEMPLATE = "# --- USER CODE START ---\n{code}\n# --- USER CODE END ---\n"

async def run_python_sandboxed(code: str, timeout_s: int = 6):
    fname = SANDBOX_DIR / f"job_{uuid.uuid4().hex}.py"
    fname.write_text(SANDBOX_TEMPLATE.format(code=code))
    out = fname.with_suffix(".out.txt")
    cmd = f"{PY} {shlex.quote(str(fname))} > {shlex.quote(str(out))} 2>&1"
    try:
        proc = await asyncio.create_subprocess_shell(cmd)
        await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        ok = proc.returncode == 0
        logs = out.read_text(errors="ignore") if out.exists() else ""
        return ok, out, logs
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return False, out, "timeout"
