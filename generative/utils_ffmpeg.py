import shutil, subprocess
def ensure_ffmpeg():
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg not found in PATH")
def run_ffmpeg(args: str):
    ensure_ffmpeg()
    subprocess.run(args, shell=True, check=True)
