"""Issue #292: ``voi.sweep(..., jobs=2)`` after the persistent pool exists.

Before #292, ``voi._run_all`` built its own bare ``ProcessPoolExecutor`` (the
implicit ``fork`` start method on Linux < 3.14) while optimize's persistent
pool was still alive -- its manager and queue-feeder threads running in the
parent. Forking a multi-threaded process can hand the child a lock another
thread held at that instant, and CPython 3.12+ warns about exactly that:
"This process ... is multi-threaded, use of fork() may lead to deadlocks in the
child." Measured on the pre-#292 tree: two such warnings per ``voi.sweep``
under this setup. That warning is the detector here.

After #292, VOI dispatches through ``process_pool.map_ordered``: it reuses
(resizes) the ONE persistent pool, whose forkserver workers are never forked
from the threaded parent, and the sweep shuts the pool down when it returns.
"""

import os
import sys
import threading
import warnings
from concurrent.futures.process import BrokenProcessPool
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import optimize  # noqa: E402
import process_pool  # noqa: E402
import voi  # noqa: E402
from test_voi_661 import _couple_contract, _tfsa_pointer  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_pool():
    process_pool.shutdown_pool()
    yield
    process_pool.shutdown_pool()
    optimize.set_workers(1)


def _doc():
    doc = _couple_contract()
    doc["provenance"] = {
        _tfsa_pointer(doc): {"confidence": "stated", "plausible_range": [0, 400000]},
    }
    return doc


def _signature(report):
    return (
        [(f.pointer, f.spread, f.strategy_spread, f.strategies_moved) for f in report.ranked],
        [(f.pointer, f.spread, f.strategy_spread, f.strategies_moved) for f in report.inert],
        [f.pointer for f in report.unread],
        report.unranked_pointers,
        report.dropped_pointers,
        report.baseline_score,
    )


@pytest.mark.timeout(900)
def test_voi_sweep_jobs2_after_persistent_pool():
    doc = _doc()
    serial = voi.sweep(doc, jobs=1, max_leaves=2, cross_objective=False)

    # The persistent pool exists, as it does after any parallel optimize sweep.
    optimize.set_workers(2)
    assert process_pool.map_ordered(abs, [-1, -2], ["a", "b"], width=2,
                                    task_timeout_s=60) == [1, 2]
    assert process_pool._POOL is not None
    # Non-vacuity: the parent IS multi-threaded right now -- the condition
    # under which a fork is dangerous.
    assert threading.active_count() > 1

    with mock.patch.object(process_pool, "map_ordered",
                           wraps=process_pool.map_ordered) as spy, \
            mock.patch.object(process_pool, "get_pool",
                              wraps=process_pool.get_pool) as pool_spy:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            parallel = voi.sweep(doc, jobs=2, max_leaves=2, cross_objective=False)

    fork_warnings = [str(w.message) for w in caught if "use of fork()" in str(w.message)]
    assert fork_warnings == [], fork_warnings
    assert any(len(c.args[1]) > 1 for c in spy.call_args_list), (
        "VOI must really have dispatched a multi-config batch through the pool")
    assert [c.args[0] for c in pool_spy.call_args_list] and all(
        c.args[0] == 2 for c in pool_spy.call_args_list), (
        "the batch must have run on a real 2-wide pool, not serially")
    assert _signature(parallel) == _signature(serial)
    assert process_pool._POOL is None, "the sweep must shut the pool down when it returns"


@pytest.mark.timeout(600)
def test_voi_run_all_reuses_single_pool(monkeypatch):
    """With a pool of another width alive, ``_run_all(jobs=2)`` resizes the ONE
    pool: at the moment the new executor is constructed, no other is alive."""
    old = process_pool.get_pool(3)
    real = process_pool.ProcessPoolExecutor
    constructed = []

    def spy(*args, **kwargs):
        constructed.append((process_pool._POOL, old._shutdown_thread))
        return real(*args, **kwargs)

    monkeypatch.setattr(process_pool, "ProcessPoolExecutor", spy)
    cfg = voi._mapped_config(_doc())
    out = voi._run_all([cfg, cfg], None, 2, ["first", "second"])
    assert constructed == [(None, True)], constructed
    assert out[0] == out[1] and out[0]
    assert process_pool._POOL_WIDTH == 2


def test_voi_worker_matches_in_process_scores():
    """``voi._worker`` only ever runs inside pool workers, where coverage does not
    measure it; call it directly so its contract is checked in-process."""
    cfg = voi._mapped_config(_doc())
    assert voi._worker((cfg, None)) == voi._strategy_scores(cfg, None)


def test_voi_run_all_propagates_pool_failures():
    """DP#32: a stalled or broken pool is never turned into a VOI result."""
    for exc in (process_pool.PoolStallError("stalled"), BrokenProcessPool("dead")):
        with mock.patch.object(process_pool, "map_ordered", side_effect=exc):
            with pytest.raises(type(exc)):
                voi._run_all([{}, {}], None, 2, ["a", "b"])


def test_voi_sweep_shuts_the_pool_down_even_when_it_raises():
    process_pool.get_pool(2)
    with mock.patch.object(voi, "_sweep", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError, match="boom"):
            voi.sweep({}, jobs=2)
    assert process_pool._POOL is None


def test_voi_cli_refuses_a_malformed_task_timeout_up_front(monkeypatch):
    monkeypatch.setenv("OPTIMIZE_TASK_TIMEOUT", "nan")
    with pytest.raises(ValueError, match="OPTIMIZE_TASK_TIMEOUT"):
        voi.main(["--input", "/nonexistent/contract.json"])
