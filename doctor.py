import os, importlib, json, pathlib

ROOT = pathlib.Path(__file__).parent

def exists(path): return (ROOT / path).exists()

dirs = ["data","data/uploads","data/clips","gui","routes","modules","services","voice","memory"]
files = ["simian_launcher.py","simian_launcher.spec","gui/simian_gui.py","modules/screen_recorder.py","services/file_scanner.py","routes/chat.py"]

imports = {}
def try_import(name):
    try:
        importlib.invalidate_caches()
        importlib.import_module(name)
        return "OK"
    except Exception as e:
        return f"ERROR: {e}"

imports["gui.simian_gui"] = try_import("gui.simian_gui")
imports["modules.screen_recorder"] = try_import("modules.screen_recorder")
imports["services.file_scanner"] = try_import("services.file_scanner")
imports["routes.chat"] = try_import("routes.chat")

print(json.dumps({
  "cwd": str(ROOT),
  "dirs": {d: exists(d) for d in dirs},
  "files": {f: exists(f) for f in files},
  "imports": imports
}, indent=2))
