import shutil
import subprocess
from datetime import datetime
from pathlib import Path

class ScreenRecorder:
    def __init__(self, clips_dir: str = "data/clips"):
        self.clips_dir = Path(clips_dir)
        self.clips_dir.mkdir(parents=True, exist_ok=True)
        self.proc = None
        self.output_path = None

    def _find_ffmpeg(self) -> str:
        if shutil.which("ffmpeg"):
            return "ffmpeg"
        cand = Path("ffmpeg-7.1.1/bin/ffmpeg.exe")
        if cand.exists():
            return str(cand)
        raise FileNotFoundError("ffmpeg not found. Add to PATH or put at 'ffmpeg-7.1.1/bin/ffmpeg.exe'")

    def start(self) -> str:
        if self.proc and self.proc.poll() is None:
            raise RuntimeError("Recording already in progress")
        ff = self._find_ffmpeg()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.output_path = self.clips_dir / f"simian_{ts}.mp4"
        cmd = [ff, "-y", "-f", "gdigrab", "-framerate", "30", "-i", "desktop", "-pix_fmt", "yuv420p", str(self.output_path)]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return str(self.output_path)

    def stop(self) -> str:
        if not self.proc or self.proc.poll() is not None:
            raise RuntimeError("No active recording")
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
        return str(self.output_path)

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None
