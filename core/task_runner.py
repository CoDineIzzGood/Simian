from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional, TypeVar

_T = TypeVar("_T")


class TaskRunner:
    """Central helper for running blocking work off the event loop/UI thread."""

    def __init__(self, max_workers: int = 4):
        self._executor = ThreadPoolExecutor(max_workers=max(1, int(max_workers)), thread_name_prefix="simian-task")

    async def run_blocking(self, fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, lambda: fn(*args, **kwargs))

    def start_background(
        self,
        fn: Callable[..., _T],
        *args: Any,
        on_done: Optional[Callable[[_T], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
        **kwargs: Any,
    ) -> None:
        """Run work in a background thread. Errors are surfaced via callback."""

        def _wrapped() -> None:
            try:
                result = fn(*args, **kwargs)
                if on_done:
                    on_done(result)
            except Exception as exc:  # nosec - intentionally surfaced through callback
                if on_error:
                    on_error(exc)

        self._executor.submit(_wrapped)


_default_runner: TaskRunner | None = None


def get_task_runner() -> TaskRunner:
    global _default_runner
    if _default_runner is None:
        _default_runner = TaskRunner()
    return _default_runner
