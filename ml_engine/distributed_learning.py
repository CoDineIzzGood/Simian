"""
Distributed training helper (optional).

Uses Ray if installed. If Ray isn't installed, this module still imports cleanly
and will raise a helpful error when used.

This is intentionally minimal: it can run multiple identical training jobs in parallel.
Later you can extend this to shard data, hyperparameter sweeps, etc.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, List

try:
    import ray  # type: ignore
except Exception:  # pragma: no cover
    ray = None

from ml_engine.train_utils import train_model


def _require_ray():
    if ray is None:
        raise RuntimeError("Ray is not installed. Install with: pip install ray")


def run_distributed(model_name: str, X, y, params: Optional[Dict[str, Any]] = None, workers: int = 4) -> List[Any]:
    """
    Run `workers` parallel training jobs and return a list of results.
    """
    _require_ray()
    ray.init(ignore_reinit_error=True)

    @ray.remote
    def remote_train(_model_name, _X, _y, _params=None):
        return train_model(_model_name, _X, _y, _params)

    futures = [remote_train.remote(model_name, X, y, params) for _ in range(max(1, int(workers)))]
    results = ray.get(futures)

    ray.shutdown()
    return results
