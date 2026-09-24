"""process_pool (issue #292): the one pool helper optimize.py and voi.py share.

Each property the module promises is driven against REAL worker processes
(forkserver), not a mock: the start method, one-pool-at-a-time, input-order
results, out-of-process dispatch, the stall bound that raises instead of
hanging, and failures that stay failures. Task functions are stdlib callables
or module-level helpers below, because a forkserver worker imports the task by
name (a lambda or closure would not pickle).
"""

import faulthandler
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures.process import BrokenProcessPool
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import process_pool  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _sleep_then_return(seconds):
    time.sleep(seconds)
    return seconds


def _pid_of(_item):
    return os.getpid()


@pytest.fixture(autouse=True)
def _no_pool_leaks():
    process_pool.shutdown_pool()
    yield
    # Kill, never wait: if a regression let a task hang (the very thing these
    # tests guard), a waiting shutdown here would hang the session after the
    # pytest-timeout bound already failed the test.
    if process_pool._POOL is not None:
        process_pool._discard_pool(process_pool._POOL)


def _wait_gone(pids, within_s=10.0):
    """Poll until every pid is gone; return the ones still alive after ``within_s``."""
    deadline = time.monotonic() + within_s
    alive = list(pids)
    while alive and time.monotonic() < deadline:
        still = []
        for pid in alive:
            try:
                os.kill(pid, 0)
                still.append(pid)
            except ProcessLookupError:
                pass
        alive = still
        if alive:
            time.sleep(0.1)
    return alive


# ── Start method and pool identity ──────────────────────────────────────────

@pytest.mark.timeout(120)
def test_pool_uses_forkserver_at_runtime():
    assert process_pool.get_pool(2)._mp_context.get_start_method() == "forkserver"


@pytest.mark.timeout(120)
def test_get_pool_reuses_same_width_and_replaces_other_width(monkeypatch):
    """Two pools never coexist: a new width shuts the old pool down BEFORE the
    new executor is constructed (checked at construction time by a spy)."""
    first = process_pool.get_pool(2)
    assert process_pool.get_pool(2) is first

    real = process_pool.ProcessPoolExecutor
    seen = []

    def spy(*args, **kwargs):
        seen.append((first._shutdown_thread, process_pool._POOL))
        return real(*args, **kwargs)

    monkeypatch.setattr(process_pool, "ProcessPoolExecutor", spy)
    second = process_pool.get_pool(3)
    assert second is not first
    assert seen == [(True, None)], (
        "the old pool must be shut down and forgotten before the new one is built")
    assert process_pool._POOL is second and process_pool._POOL_WIDTH == 3


def test_get_pool_refuses_a_zero_width():
    with pytest.raises(ValueError, match="width"):
        process_pool.get_pool(0)


def test_shutdown_pool_without_a_pool_is_a_noop():
    process_pool.shutdown_pool()
    assert process_pool._POOL is None and process_pool._POOL_WIDTH is None


# ── map_ordered: order, real dispatch, labels ───────────────────────────────

@pytest.mark.timeout(120)
def test_map_ordered_preserves_input_order():
    """The FIRST item finishes LAST; results still come back in input order."""
    items = [0.6, 0.3, 0.0]
    out = process_pool.map_ordered(_sleep_then_return, items, ["a", "b", "c"],
                                   width=3, task_timeout_s=60)
    assert out == items


@pytest.mark.timeout(120)
def test_map_ordered_really_runs_out_of_process():
    pids = process_pool.map_ordered(_pid_of, [0, 1, 2, 3], list("abcd"),
                                    width=2, task_timeout_s=60)
    assert all(pid != os.getpid() for pid in pids), pids


def test_labels_length_mismatch_refused():
    with pytest.raises(ValueError, match="one label per item"):
        process_pool.map_ordered(abs, [1, 2], ["only-one"], width=2, task_timeout_s=60)
    assert process_pool._POOL is None, "refused before any pool was built"


@pytest.mark.timeout(120)
def test_in_worker_true_inside_pool_false_here():
    assert process_pool.in_worker() is False
    assert process_pool.get_pool(1).submit(process_pool.in_worker).result(timeout=60) is True


# ── Liveness: a stall fails loudly and promptly ─────────────────────────────

@pytest.mark.timeout(60)
def test_stall_raises_pool_stall_error_naming_task():
    t0 = time.monotonic()
    with pytest.raises(process_pool.PoolStallError) as info:
        process_pool.map_ordered(time.sleep, [0, 3600], ["fast", "the-stalled-one"],
                                 width=2, task_timeout_s=2)
    elapsed = time.monotonic() - t0
    msg = str(info.value)
    assert elapsed < 2 + 15, f"the stall took {elapsed:.1f}s to surface"
    assert "the-stalled-one" in msg and "task 1 " in msg and "2s" in msg, msg
    pids = [int(p) for p in msg.split("workers [", 1)[1].split("]", 1)[0].split(",")]
    assert pids, msg
    for pid in pids:
        assert str(pid) in msg
    assert _wait_gone(pids) == [], "stalled workers must be killed"
    assert process_pool._POOL is None, "a stalled pool must be discarded"


@pytest.mark.timeout(120)
def test_stalled_interpreter_exits_instead_of_hanging():
    """No hang at interpreter exit either: atexit/concurrent.futures joins must
    not wait on the killed workers."""
    script = (
        "import sys, time\n"
        f"sys.path.insert(0, {REPO_ROOT!r})\n"
        "import process_pool\n"
        "process_pool.map_ordered(time.sleep, [3600, 3600], ['a', 'b'],"
        " width=2, task_timeout_s=2)\n"
    )
    # Own session, so a regression that hangs is killed WITH its forkserver
    # and workers instead of leaving them orphaned on the machine.
    proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        _out, err = proc.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise
    assert proc.returncode != 0
    assert "PoolStallError" in err
    assert "Thread 0x" in err and 'File "' in err, err


# ── Failures stay failures ──────────────────────────────────────────────────

@pytest.mark.timeout(120)
def test_worker_exception_propagates_unchanged():
    with pytest.raises(ValueError, match="invalid literal"):
        process_pool.map_ordered(int, ["x"], ["bad"], width=2, task_timeout_s=60)


@pytest.mark.timeout(120)
def test_worker_exception_cancels_the_rest_and_keeps_the_pool():
    with pytest.raises(ValueError):
        process_pool.map_ordered(int, ["x", "1", "2"], ["bad", "b", "c"],
                                 width=1, task_timeout_s=60)
    # A task exception is not a broken pool: the pool still serves.
    assert process_pool.map_ordered(abs, [-4], ["ok"], width=1, task_timeout_s=60) == [4]


@pytest.mark.timeout(120)
def test_dead_worker_raises_broken_pool_and_resets():
    with pytest.raises(BrokenProcessPool):
        process_pool.map_ordered(os._exit, [1], ["dies"], width=2, task_timeout_s=60)
    assert process_pool._POOL is None
    assert process_pool.map_ordered(abs, [-3, -5], ["a", "b"], width=2,
                                    task_timeout_s=60) == [3, 5]


# ── available_cpus: affinity-aware, None stays None ─────────────────────────

def test_available_cpus_ladder():
    both = SimpleNamespace(process_cpu_count=lambda: 3,
                           sched_getaffinity=lambda pid: {0, 1}, cpu_count=lambda: 16)
    assert process_pool.available_cpus(both) == 3
    affinity = SimpleNamespace(sched_getaffinity=lambda pid: {0, 1}, cpu_count=lambda: 16)
    assert process_pool.available_cpus(affinity) == 2
    plain = SimpleNamespace(cpu_count=lambda: 8)
    assert process_pool.available_cpus(plain) == 8
    unknown = SimpleNamespace(process_cpu_count=lambda: None,
                              sched_getaffinity=lambda pid: {0, 1}, cpu_count=lambda: 16)
    assert process_pool.available_cpus(unknown) is None, (
        "an undeterminable count must stay None, not borrow another source's number")
    assert process_pool.available_cpus(SimpleNamespace(cpu_count=lambda: None)) is None


def test_available_cpus_honours_the_real_affinity_mask():
    if not hasattr(os, "sched_getaffinity"):
        pytest.skip("no affinity API on this platform")
    assert process_pool.available_cpus() == len(os.sched_getaffinity(0))


# ── OPTIMIZE_TASK_TIMEOUT parsing ───────────────────────────────────────────

def test_task_timeout_from_env():
    assert process_pool.task_timeout_from_env(None) == 3600.0
    assert process_pool.task_timeout_from_env("") == 3600.0
    assert process_pool.task_timeout_from_env("   ") == 3600.0
    assert process_pool.task_timeout_from_env("5") == 5.0
    for bad in ("abc", "0", "-1", "nan", "inf", "-inf"):
        with pytest.raises(ValueError, match="OPTIMIZE_TASK_TIMEOUT"):
            process_pool.task_timeout_from_env(bad)


# ── Worker-only code, covered in-process ────────────────────────────────────

def test_worker_init_registers_sigusr1():
    process_pool._worker_init()
    assert faulthandler.unregister(signal.SIGUSR1) is True, (
        "a worker must dump its stacks on SIGUSR1")


@pytest.mark.timeout(120)
def test_pool_threads_are_alive_while_the_pool_is():
    """Why a second fork next to a live pool was dangerous (#292): the parent is
    multi-threaded while a pool is alive, and single-threaded after shutdown."""
    before = threading.active_count()
    process_pool.map_ordered(abs, [-1, -2], ["a", "b"], width=2, task_timeout_s=60)
    assert threading.active_count() > before
    process_pool.shutdown_pool()
    assert threading.active_count() == before


def test_dump_stacks_skips_dead_and_vanished_workers(monkeypatch):
    """The stall path asks each LIVE worker for its stacks. A worker that is
    already dead, or that vanishes between is_alive() and the signal, has no
    stack to give and must not turn the stall report into a crash."""
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()                                   # reaped: its pid no longer exists
    dead = SimpleNamespace(pid=gone.pid, is_alive=lambda: False)
    vanished = SimpleNamespace(pid=gone.pid, is_alive=lambda: True)
    fake_pool = SimpleNamespace(_processes={1: dead, 2: vanished})
    monkeypatch.setattr(process_pool, "_STACK_DUMP_GRACE_S", 0.0)
    assert process_pool._dump_stacks(fake_pool) == []
