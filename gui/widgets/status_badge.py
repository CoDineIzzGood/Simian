from __future__ import annotations

import customtkinter as ctk

_STATUS_COLORS = {
    "ok": "#2e9f4d",
    "degraded": "#d8a100",
    "error": "#cc3a3a",
    "unknown": "#6f7784",
}


class StatusBadge(ctk.CTkFrame):
    def __init__(self, master, label: str, status: str = "unknown", **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self._text = ctk.CTkLabel(self, text=label)
        self._text.pack(side="left", padx=(0, 6))
        self._dot = ctk.CTkLabel(self, text="●", width=14)
        self._dot.pack(side="left")
        self._state = "unknown"
        self.set_status(status)

    def set_status(self, status: str, message: str | None = None) -> None:
        state = (status or "unknown").lower()
        color = _STATUS_COLORS.get(state, _STATUS_COLORS["unknown"])
        self._dot.configure(text_color=color)
        self._state = state
        if message:
            self._text.configure(text=message)

    @property
    def status(self) -> str:
        return self._state
