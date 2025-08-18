# auto_installer.py
import sys, importlib, subprocess

IS_FROZEN = getattr(sys, "frozen", False)

def safe_import(module, pip_name=None):
    try:
        return importlib.import_module(module)
    except Exception:
        if IS_FROZEN:
            return None
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", pip_name or module])
            return importlib.import_module(module)
        except Exception:
            return None

def install_all():
    return
