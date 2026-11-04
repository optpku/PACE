from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


from .events import append_event
from .workspace import utc_now

logger = logging.getLogger(__name__)


class ProfileRecorder:
    """Small JSONL profiler for pipeline and skill-loop timing."""

    def __init__(self, *, enabled: bool = False, path: Path | None = None,
                 workdir: Path | None = None) -> None:
        self.enabled = bool(enabled)
        self.path = path
        self.workdir = Path(workdir) if workdir is not None else None
        self.events: list[dict[str, Any]] = []

    @contextmanager
    def span(
        self,
        name: str,
        *,
        iteration: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Iterator[None]:
        if not self.enabled:
            yield
            return
        start = time.perf_counter()
        try:
            yield
        finally:
            self.record(
                name,
                elapsed_seconds=time.perf_counter() - start,
                iteration=iteration,
                metadata=metadata,
            )

    def record(
        self,
        name: str,
        *,
        elapsed_seconds: float,
        iteration: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not self.enabled:
            return
        event = {
            "timestamp_utc": utc_now(),
            "name": str(name),
            "elapsed_seconds": round(float(elapsed_seconds), 6),
            "iteration": iteration,
            "metadata": metadata or {},
        }
        self.events.append(event)
        if self.path is not None and self.workdir is not None:
            try:
                append_event(self.workdir, "PROFILE_SPAN", **event)
            except Exception:
                # Timing is observational: preserve the owning task's result,
                # including KeyboardInterrupt and its recovery checkpoint.
                logger.warning("Could not persist profiling event", exc_info=True)

    def summary(self) -> dict[str, Any]:
        totals: dict[str, float] = {}
        counts: dict[str, int] = {}
        for event in self.events:
            name = str(event.get("name", ""))
            totals[name] = totals.get(name, 0.0) + float(
                event.get("elapsed_seconds") or 0.0
            )
            counts[name] = counts.get(name, 0) + 1
        return {
            "schema_version": "nl_proof_profile_summary/v1",
            "enabled": self.enabled,
            "event_count": len(self.events),
            "events_path": str(self.path) if self.path is not None else "",
            "totals_seconds": {
                name: round(value, 6) for name, value in sorted(totals.items())
            },
            "counts": {name: counts[name] for name in sorted(counts)},
        }
