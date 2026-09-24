"""The ONE place a process pool is built (issue #292).

Every parallel sweep in the optimization layer -- ``optimize._map_scenarios``
and ``voi._run_all`` -- dispatches through :func:`map_ordered` here. Nothing
else in the tree may construct a ``ProcessPoolExecutor`` (DP#9: one helper, no
second copy); ``tests/architecture/test_pool_start_method.py`` enforces that.

WHAT THIS MODULE GUARANTEES
---------------------------
1. **An explicit, non-fork start method.** Workers are started with
   ``multiprocessing.get_context(START_METHOD)`` (``forkserver``), never the
   implicit ``fork`` that Linux uses before Python 3.14. Forking a process that
   already runs threads (a live pool's manager and queue-feeder threads) can hand
   the child a lock another thread held at fork time -- the deadlock CPython 3.12
   warns about with "use of fork() may lead to deadlocks". Forkserver workers are
   forked from a single-threaded server process, so that cannot happen, and the
   behaviour is the same on every supported Python (it is 3.14's default).

   Consequence: a worker does NOT inherit the parent's in-memory state. It
   re-imports the module that owns the task function, so anything a task needs
   must be reachable by import (module-level functions, import-time registration
   such as ``countries.canada``'s providers). A monkeypatch in the parent does not
   reach a worker.

2. **At most one pool alive at a time.** :func:`get_pool` keeps ONE persistent
   pool keyed by width. Asking for another width shuts the old pool down BEFORE
   the new one is built, so a second pool is never forked next to a live one.

3. **A liveness bound (DP#32).** :func:`map_ordered` waits for each result, in
   input order, at most ``task_timeout_s`` seconds. When that bound expires it
   dumps the Python stacks of the parent and of every worker to stderr, kills the
   workers, discards the pool and raises :class:`PoolStallError` naming the
   stalled task. A hung worker can no longer freeze the run silently.

4. **Results in input order.** Results are collected by index, never as they
   complete, so a parallel sweep feeds its deterministic sort exactly what the
   serial path does (DP#23: ``--json`` stays byte-identical).

5. **Failures stay failures.** A worker exception is re-raised unchanged, a dead
   worker surfaces as ``BrokenProcessPool``, and a stall as ``PoolStallError``.
   None of them is ever turned into a score, an empty list or a refusal marker.

This module imports only the standard library (DP#25: it is infrastructure the
optimization layer uses; it knows nothing about scenarios or objectives).
"""

from __future__ import annotations

import atexit
import concurrent.futures
import faulthandler
import math
import multiprocessing
import os
import signal
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from typing import Any, Callable, List, Optional, Sequence

#: The start method of every worker. ``fork`` is deliberately not allowed here
#: (see the module docstring); the architecture guard pins this value.
START_METHOD = "forkserver"

#: Default per-task liveness bound, in seconds, when ``OPTIMIZE_TASK_TIMEOUT``
#: is unset or blank. Generous on purpose: it exists to turn a hang into an
#: error, not to police slow-but-progressing work.
DEFAULT_TASK_TIMEOUT_S = 3600.0

#: How long a stalled worker is given to write its stacks after SIGUSR1,
#: before it is killed.
_STACK_DUMP_GRACE_S = 1.0

_POOL: Optional[ProcessPoolExecutor] = None
_POOL_WIDTH: Optional[int] = None
_ATEXIT_REGISTERED = False


class PoolStallError(RuntimeError):
    """A pool task did not finish within its liveness bound. The message names
    the task (index and label), the bound, and the worker pids whose stacks
    were written to stderr."""


def available_cpus(osmod: Any = os) -> Optional[int]:
    """How many CPUs THIS process may run on, or None when undeterminable.

    ``os.cpu_count()`` reports the machine and ignores the affinity mask
    (``taskset``, a container's cpuset), so a pool sized from it oversubscribes
    a restricted process (#292: 15 workers under a 2-CPU mask). Preference order:
    ``os.process_cpu_count()`` (3.13+, affinity-aware), then
    ``len(os.sched_getaffinity(0))`` (Linux), then ``os.cpu_count()`` (platforms
    with neither). A None from the chosen source is returned as None -- never
    replaced by another source's number or a guess (DP#32); the caller decides
    what an unknown count means."""
    if hasattr(osmod, "process_cpu_count"):
        return osmod.process_cpu_count()
    if hasattr(osmod, "sched_getaffinity"):
        return len(osmod.sched_getaffinity(0))
    return osmod.cpu_count()


def in_worker() -> bool:
    """True inside a pool worker. A task running in a worker must never open a
    second, nested pool; callers take their serial path instead."""
    return multiprocessing.parent_process() is not None


def task_timeout_from_env(env_val: Optional[str]) -> float:
    """The per-task liveness bound from ``OPTIMIZE_TASK_TIMEOUT`` (seconds).

    Unset or blank -> :data:`DEFAULT_TASK_TIMEOUT_S`. Anything else must be a
    finite number > 0: a typo, zero, a negative, NaN or infinity is REFUSED with
    a ValueError naming the variable, never coerced (DP#13/DP#32) -- a zero
    would make every task "stall", and NaN/inf would silently remove the bound."""
    if env_val is None or env_val.strip() == "":
        return DEFAULT_TASK_TIMEOUT_S
    try:
        value = float(env_val)
    except ValueError:
        raise ValueError(
            f"OPTIMIZE_TASK_TIMEOUT must be a number of seconds > 0, got {env_val!r}"
        ) from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError(
            f"OPTIMIZE_TASK_TIMEOUT must be a finite number of seconds > 0, got {env_val!r}"
        )
    return value


def _worker_init() -> None:
    """Runs once in every worker: SIGUSR1 makes the worker write the stack of
    every thread to stderr, so a stall can be diagnosed before the kill."""
    faulthandler.register(signal.SIGUSR1, all_threads=True)


def get_pool(width: int) -> ProcessPoolExecutor:
    """The persistent pool, ``width`` workers wide.

    Reused while alive at the same width. Any other width shuts the current pool
    down FIRST and then builds a new one, so two pools never coexist. This is the
    only construction of a ``ProcessPoolExecutor`` in the tree."""
    global _POOL, _POOL_WIDTH, _ATEXIT_REGISTERED
    if width < 1:
        raise ValueError(f"pool width must be >= 1, got {width}")
    if _POOL is not None and _POOL_WIDTH == width:
        return _POOL
    shutdown_pool()
    _POOL = ProcessPoolExecutor(
        max_workers=width,
        mp_context=multiprocessing.get_context(START_METHOD),
        initializer=_worker_init,
    )
    _POOL_WIDTH = width
    if not _ATEXIT_REGISTERED:
        atexit.register(shutdown_pool)
        _ATEXIT_REGISTERED = True
    return _POOL


def shutdown_pool() -> None:
    """Shut the persistent pool down (waiting for its workers) and forget it.
    A no-op when no pool is alive."""
    global _POOL, _POOL_WIDTH
    pool = _POOL
    _POOL = None
    _POOL_WIDTH = None
    if pool is not None:
        pool.shutdown(wait=True)


def _discard_pool(pool: ProcessPoolExecutor) -> List[int]:
    """Kill every worker of ``pool`` and forget it WITHOUT waiting on it (a
    stalled or broken pool must not be able to hang the parent). Returns the
    killed pids. ``pool._processes`` is private; if a future CPython renames
    it, this raises AttributeError -- loudly -- rather than leaving hung
    workers behind."""
    global _POOL, _POOL_WIDTH
    procs = list(dict(pool._processes).values()) if pool._processes is not None else []
    for proc in procs:
        proc.kill()
    pool.shutdown(wait=False, cancel_futures=True)
    for proc in procs:
        proc.join(timeout=5)
    if _POOL is pool:
        _POOL = None
        _POOL_WIDTH = None
    return [proc.pid for proc in procs]


def _dump_stacks(pool: ProcessPoolExecutor) -> List[int]:
    """Write the parent's and every worker's thread stacks to stderr. Returns
    the worker pids that were asked for their stacks."""
    procs = list(dict(pool._processes).values()) if pool._processes is not None else []
    sys.stderr.flush()
    os.write(2, b"\n=== process_pool: stalled task -- parent stacks ===\n")
    # fd 2, not sys.stderr: faulthandler needs a real file descriptor, and the
    # stacks must reach the process's stderr even when sys.stderr is replaced.
    faulthandler.dump_traceback(file=2, all_threads=True)
    pids = []
    for proc in procs:
        if not proc.is_alive():
            continue
        try:
            os.kill(proc.pid, signal.SIGUSR1)
        except ProcessLookupError:
            # It exited between is_alive() and kill(): it has no stack left to
            # dump. The stall itself is still raised by the caller.
            continue
        pids.append(proc.pid)
    time.sleep(_STACK_DUMP_GRACE_S)
    return pids


def map_ordered(fn: Callable[[Any], Any], items: Sequence[Any],
                labels: Sequence[str], *, width: int,
                task_timeout_s: float) -> List[Any]:
    """``[fn(x) for x in items]`` across the persistent pool, IN INPUT ORDER.

    ``labels[i]`` names ``items[i]`` in a stall error; it must be given for
    every item. ``fn`` must be importable by name in a worker (a module-level
    function), because workers are started with :data:`START_METHOD`.

    Liveness: result ``i`` is awaited at most ``task_timeout_s`` seconds after
    result ``i - 1`` arrived. Tasks start in submission order, so by then task
    ``i`` is already running: a task trips the bound only when it has itself been
    running for about ``task_timeout_s`` seconds, never because it queued behind
    others. On a trip the stacks go to stderr, the workers are killed, the pool
    is discarded and :class:`PoolStallError` is raised.

    A worker exception is re-raised unchanged (the remaining tasks are
    cancelled); a dead worker raises ``BrokenProcessPool`` and the pool is
    discarded."""
    if len(labels) != len(items):
        raise ValueError(
            f"map_ordered needs one label per item: {len(items)} items, {len(labels)} labels")
    pool = get_pool(width)
    futures = [pool.submit(fn, item) for item in items]
    results: List[Any] = []
    try:
        for index, fut in enumerate(futures):
            done, _ = concurrent.futures.wait([fut], timeout=task_timeout_s)
            if not done:
                asked = _dump_stacks(pool)
                killed = _discard_pool(pool)
                raise PoolStallError(
                    f"pool task {index} ({labels[index]!r}) did not finish within "
                    f"{task_timeout_s}s (OPTIMIZE_TASK_TIMEOUT); workers {killed} were "
                    f"killed; stacks of the parent and of workers {asked} were written "
                    f"to stderr")
            results.append(fut.result())
    except BrokenProcessPool:
        _discard_pool(pool)
        raise
    except BaseException:
        for fut in futures:
            fut.cancel()
        raise
    return results
