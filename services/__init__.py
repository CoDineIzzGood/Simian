"""Compatibility shim for older imports.

Some parts of the codebase (notably certain GUI builds) import:

    from services.services.mic_listener import MicListenerService
    from services.services.file_scanner import FileScannerService

But the project layout also supports importing directly from `services/...`.

This subpackage exists to keep those imports working without forcing a global
refactor. New code should prefer `services.mic_listener` and
`services.file_scanner` directly.
"""

from .mic_listener import MicListenerService
from .file_scanner import FileScannerService

__all__ = [
    "MicListenerService",
    "FileScannerService",
]
