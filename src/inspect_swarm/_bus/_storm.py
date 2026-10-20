import math
from collections import deque
from dataclasses import dataclass

import anyio

from ._record import Record


# A channel's storm controls, set from its factory's parameters: the most UTF-8 bytes a
# payload may have, and how many records one member may commit in a sliding window of
# seconds. `None` disables either. They are bus rules, not Inspect limits: a violation
# rejects the record with a `ToolError` and never raises `LimitExceededError`.
@dataclass(frozen=True)
class StormControls:
    max_bytes: int | None = None
    rate: tuple[int, float] | None = None

    def __post_init__(self) -> None:
        if self.max_bytes is not None and self.max_bytes < 1:
            raise ValueError(
                f"StormControls: max_bytes must be at least 1, got {self.max_bytes}."
            )
        if self.rate is not None:
            count, window = self.rate
            if count < 1 or not (0 < window < math.inf):
                raise ValueError(
                    f"StormControls: rate must be a positive count and a finite, positive window in seconds, got {tuple(self.rate)}."
                )


# One channel's storm controls in one run: the commit times of each member's records,
# for the rate.
class Storm:
    def __init__(self, controls: StormControls) -> None:
        self.controls = controls
        self._commits: dict[str, deque[float]] = {}

    def check(self, record: Record) -> tuple[str, str] | None:
        # the control a record violates and the reason, or None
        max_bytes = self.controls.max_bytes
        if max_bytes is not None:
            size = len(record.payload.encode("utf-8"))
            if size > max_bytes:
                return (
                    "max_bytes",
                    f"{record.kind} is {size} bytes; the limit is {max_bytes}",
                )
        if self.controls.rate is not None:
            count, window = self.controls.rate
            commits = self._window(record.sender, window)
            if len(commits) >= count:
                wait = max(1, math.ceil(commits[0] + window - anyio.current_time()))
                return ("rate", f"rate limit: retry in {wait} s")
        return None

    def commit(self, record: Record) -> None:
        if self.controls.rate is not None:
            self._commits.setdefault(record.sender, deque()).append(
                anyio.current_time()
            )

    def _window(self, sender: str, window: float) -> deque[float]:
        commits = self._commits.setdefault(sender, deque())
        now = anyio.current_time()
        while commits and commits[0] <= now - window:
            commits.popleft()
        return commits
