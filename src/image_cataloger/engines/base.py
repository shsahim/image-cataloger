"""Engine interface shared by the API and Local-CLI engines."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Protocol


@dataclass
class DescribeResult:
    path: str
    summary: str | None = None
    error: str | None = None
    status: str = "ok"  # ok | unreadable | error
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


class RunControl:
    """Pause/resume/stop flags shared between the GUI, the runner and the engines."""

    def __init__(self) -> None:
        self._resume = threading.Event()
        self._resume.set()
        self._stop = threading.Event()

    def pause(self) -> None:
        self._resume.clear()

    def resume(self) -> None:
        self._resume.set()

    def stop(self) -> None:
        self._stop.set()
        self._resume.set()  # unblock anyone waiting on pause

    @property
    def paused(self) -> bool:
        return not self._resume.is_set() and not self._stop.is_set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def wait_if_paused(self) -> bool:
        """Block while paused. Returns False if the run was stopped meanwhile."""
        self._resume.wait()
        return not self._stop.is_set()

    def sleep(self, seconds: float) -> bool:
        """Interruptible sleep used for backoff. Returns False when stopped."""
        end = threading.Event()
        step = 0.25
        waited = 0.0
        while waited < seconds:
            if self._stop.is_set():
                return False
            end.wait(min(step, seconds - waited))
            waited += step
        return self.wait_if_paused()


class Engine(Protocol):
    name: str
    model: str

    def describe(self, paths: list[str], control: RunControl) -> list[DescribeResult]:
        """Describe each path. Must return exactly one result per input path."""
        ...
