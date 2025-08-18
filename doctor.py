import importlib, os, pkgutil, json

expected_dirs = [
  "data", "data/uploads", "data/clips",
  "gui", "routes", "modules", "services", "voice",
  "memory"
]

expected_files = [
  "simian_launcher.py", "simian_launcher.spec",
  "gui/simian_gui.py",
  "modules/screen_recorder.py",
  "services/file_scanner.py",
  "routes/gui.py",
]

report = {
  "cwd": os.getcwd(),
  "dirs": {},
  "files": {},
  "imports": {},
  "chat_route_file_guess": None
}

for d in expected_dirs:
    report["dirs"][d] = os.path.isdir(d)

for f in expected_files:
    report["files"][f] = os.path.isfile(f)

for mod in ["gui.simian_gui","modules.screen_recorder","services.file_scanner","routes.gui"]:
    try:
        importlib.import_module(mod)
        report["imports"][mod] = "OK"
    except Exception as e:
        report["imports"][mod] = f"ERROR: {e.__class__.__name__}: {e}"

# try to guess which routes file defines /chat
guess = None
if os.path.isdir("routes"):
    for _, name, _ in pkgutil.iter_modules(["routes"]):
        if name in ("gpt","chat","api"):
            candidate = os.path.join("routes", f"{name}.py")
            if os.path.exists(candidate):
                guess = candidate
                break
report["chat_route_file_guess"] = guess

print(json.dumps(report, indent=2))
