# simian_launcher.spec
import os
from importlib import util as iu
from PyInstaller.utils.hooks import collect_submodules

# Ensure runtime dirs exist (both at build-time and later in dist/)
for d in ("data", os.path.join("data", "uploads"), os.path.join("data", "clips")):
    os.makedirs(d, exist_ok=True)

def hasmod(m: str) -> bool:
    return iu.find_spec(m) is not None

hidden = [
    # our own modules
    "gui.simian_gui",
    "services.screen_recorder",
    "services.file_scanner",
    "voice.edge_tts_speak",
]

# pull in optional deps if present
for m in ["customtkinter", "requests", "speech_recognition", "pyttsx3", "edge_tts", "simpleaudio"]:
    if hasmod(m):
        try:
            hidden += collect_submodules(m)
        except Exception:
            pass

datas = []
def add_tree(path, dest):
    if os.path.exists(path):
        datas.append((path, dest))

# bundle only data trees here (code is included via imports/hiddenimports)
add_tree("data", "data")

block_cipher = None

a = Analysis(
    ['simian_launcher.py'],
    pathex=['.'],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Simian',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,   # keep console so we can see logs
)

coll = COLLECT(
    exe, a.binaries, a.zipfiles, a.datas,
    strip=False, upx=True, upx_exclude=[],
    name='Simian'
)
