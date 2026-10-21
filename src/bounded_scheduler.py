"""Bounded thread scheduling with ordered outcomes and drained interruptions.

These ephemeral records contain worker values/exceptions, not persisted contracts.
The caller must account for and propagate errors before publishing a package.
"""
from __future__ import annotations

from concurrent.futures import CancelledError, FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from threading import Event, RLock
from typing import Callable, Generic, TypeVar

T = TypeVar("T")


@dataclass
class SubmissionControl:
    stopped: Event = field(default_factory=Event)
    gate: RLock = field(default_factory=RLock)
    fatal_code: str | None = None

    def stop(self, fatal_code: str | None = None) -> None:
        # Linearize stopping against submission, including the initial window.
        with self.gate:
            if fatal_code in {"AUTHENTICATION_FAILED", "QUOTA_EXHAUSTED"} and self.fatal_code is None:
                self.fatal_code = fatal_code
            self.stopped.set()


@dataclass(frozen=True)
class Outcome(Generic[T]):
    index: int
    value: T | None = None
    error: BaseException | None = None

    @property
    def cancelled(self) -> bool:
        return isinstance(self.error, CancelledError)


@dataclass(frozen=True)
class ScheduleRun(Generic[T]):
    outcomes: tuple[Outcome[T], ...]
    interruption: BaseException | None
    max_in_flight: int


def run_bounded(
    worker: Callable[[int], T],
    total: int,
    concurrency: int,
    control: SubmissionControl,
    progress: Callable[[int], None] | None = None,
) -> ScheduleRun[T]:
    """Refill free slots; never enqueue beyond the one/two-worker window.

    Values are returned in source order. Completed values may accumulate while
    an earlier worker runs. They are completed work, not speculative futures.
    Cancellation cannot kill a running thread: stop submission, cancel only
    not-started futures, and drain/account for every submitted future instead.
    """
    if concurrency not in (1, 2):
        raise ValueError("Package concurrency must be one or two.")
    if total < 0:
        raise ValueError("Schedule size cannot be negative.")
    pending: dict[Future[T], int] = {}
    completed: dict[int, Outcome[T]] = {}
    next_index = 0
    high_water = 0
    interruption = None
    pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="azure-draft")

    def collect(future: Future[T]) -> None:
        index = pending.pop(future)
        try:
            completed[index] = Outcome(index=index, value=future.result())
        except BaseException as exc:
            completed[index] = Outcome(index=index, error=exc)
            control.stop()

    def replenish() -> None:
        nonlocal next_index, high_water
        with control.gate:
            # This short lock also lets the initial one/two submissions commit
            # before an immediate worker failure stops subsequent submissions.
            while not control.stopped.is_set() and len(pending) < concurrency and next_index < total:
                pending[pool.submit(worker, next_index)] = next_index
                next_index += 1
                high_water = max(high_water, len(pending))

    try:
        if progress:
            progress(0)
        replenish()
        while pending:
            done, _ = wait(tuple(pending), return_when=FIRST_COMPLETED)
            # Inspect the entire completed cohort before replenishing: a fast
            # success must not conceal an already-completed fatal failure.
            for future in sorted(done, key=pending.__getitem__):
                collect(future)
            if progress:
                progress(sum(not item.cancelled for item in completed.values()))
            replenish()
    except BaseException as exc:
        interruption = exc
        control.stop()
    finally:
        if control.stopped.is_set():
            for future in pending:
                future.cancel()
        pool.shutdown(wait=True, cancel_futures=True)
        for future in tuple(pending):
            collect(future)

    return ScheduleRun(tuple(completed[i] for i in sorted(completed)), interruption, high_water)
