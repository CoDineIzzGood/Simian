
# services/screen_monitor.py
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

@dataclass
class ScreenMonitor:
    interval: float = 2.0
    on_tick: Callable[[float], None] = lambda t: None
    _thread: threading.Thread | None = field(default=None, init=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        def _run():
            t0 = time.time()
            while not self._stop.is_set():
                self.on_tick(time.time() - t0)
                time.sleep(self.interval)
        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

    def stop(self):
        if self._thread:
            self._stop.set()
            self._thread.join(timeout=3)
            self._thread = None
