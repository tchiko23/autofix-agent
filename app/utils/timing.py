from __future__ import annotations

import json
import time
from contextlib import contextmanager
from typing import Callable, Iterator


class Timer:
    def __init__(self, enabled: bool = True, sink: Callable[[str], None] | None = None):
        self.enabled = enabled
        self.started = time.perf_counter()
        self.sink = sink or print

    def emit(self, step: str, elapsed_ms: float, **metadata: object) -> None:
        if not self.enabled:
            return
        payload = {"step": step, "elapsed_ms": round(elapsed_ms, 2), **metadata}
        self.sink(f"[TIME] {json.dumps(payload, ensure_ascii=False)}")


@contextmanager
def timed(timer: Timer, step: str, **metadata: object) -> Iterator[None]:
    start = time.perf_counter()
    yield
    timer.emit(step, (time.perf_counter() - start) * 1000.0, **metadata)
