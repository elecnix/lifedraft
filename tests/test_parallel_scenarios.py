"""Parallel scenario sweep == serial sweep (perf/parallel-scenarios).

The optimizer evaluates each scenario as an INDEPENDENT pure fold (DP#26), so
the outer sweeps that loop ``run_optimization`` are dispatched across worker
processes. The HARD requirement is that parallelism is a pure speedup: the
collected results must be ranked in the EXACT same order, with the EXACT same
per-row values, as the serial (``--workers 1``) path -- so a parallel run's
report is byte-identical to a serial one. These tests lock that invariant and
that the serial fallback is a real, pool-free code path.
"""

from tax_data import default_tax_provider  # noqa: F401  (import-order parity)
import os
import pickle
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import optimize
from optimize import ChargeLimitExceededError
from test_optimize import _make_test_cfg, _write_cfg_to_file
from test_issue_891_refinance_refuse_and_skip import _cfg as _refusal_cfg


def _ranked_signature(results):
    """The order-and-value signature a parallel run must reproduce exactly:
    the ranked rows' identifying keys plus their scored figures, IN ORDER."""
    return [
        (r.get('strategy'), r.get('income_scenario_id'), r.get('ltv'),
         r.get('refinance_id'), r.get('draw_fraction'),
         r.get('net_benefit'), r.get('objective_score'))
        for r in results
    ]


class TestParallelEqualsSerial(unittest.TestCase):
    """A parallel sweep ranks identically to the serial one."""

    def setUp(self):
        self.cfg = _make_test_cfg()
        self.path = _write_cfg_to_file(self.cfg)

    def tearDown(self):
        optimize._shutdown_pool()
        optimize.set_workers(1)
        os.unlink(self.path)

    def test_ltv_exploration_parallel_matches_serial(self):
        optimize.set_workers(1)
        serial = optimize.run_ltv_exploration(self.cfg, self.path)
        optimize.set_workers(4)
        parallel = optimize.run_ltv_exploration(self.cfg, self.path)
        self.assertEqual(_ranked_signature(serial), _ranked_signature(parallel),
                         "parallel LTV sweep must rank identically to serial")

    def test_income_scenario_exploration_parallel_matches_serial(self):
        optimize.set_workers(1)
        serial = optimize.run_income_scenario_exploration(self.cfg, self.path)
        optimize.set_workers(4)
        parallel = optimize.run_income_scenario_exploration(self.cfg, self.path)
        self.assertEqual(_ranked_signature(serial), _ranked_signature(parallel),
                         "parallel income sweep must rank identically to serial")


class TestSerialFallback(unittest.TestCase):
    """``--workers 1`` is a genuine pool-free path (provably == pre-parallel)."""

    def tearDown(self):
        optimize._shutdown_pool()
        optimize.set_workers(1)

    def test_workers_one_opens_no_pool(self):
        cfg = _make_test_cfg()
        path = _write_cfg_to_file(cfg)
        try:
            optimize._shutdown_pool()
            optimize.set_workers(1)
            optimize.run_ltv_exploration(cfg, path)
            self.assertIsNone(
                optimize._POOL,
                "workers=1 must take the serial path and never open a pool")
        finally:
            os.unlink(path)

    def test_map_scenarios_empty_is_noop(self):
        # Degenerate sweep: no scenarios -> no work, no pool, no error.
        optimize.set_workers(4)
        self.assertEqual(optimize._map_scenarios([]), [])
        self.assertIsNone(optimize._POOL)


class TestCliWorkersFlag(unittest.TestCase):
    """The ``--workers`` CLI flag wires into ``set_workers`` in ``main()``.

    ``--list-objectives`` short-circuits ``main()`` before any input document is
    loaded, so this exercises the ``--workers -> set_workers`` handoff cheaply
    and deterministically (no contract, no optimizer pass, no pool)."""

    def tearDown(self):
        optimize._shutdown_pool()
        optimize.set_workers(1)

    def test_workers_flag_calls_set_workers(self):
        import io
        import contextlib
        argv = ['optimize.py', '--workers', '3', '--list-objectives']
        with mock.patch.object(sys, 'argv', argv):
            with contextlib.redirect_stdout(io.StringIO()):
                optimize.main()
        self.assertEqual(optimize._WORKERS, 3,
                         "--workers N must resolve the worker count via set_workers")


class TestWorkerCountResolution(unittest.TestCase):
    """The --workers / OPTIMIZE_WORKERS / (cpu_count - 1) resolution ladder.

    ``_resolve_workers`` caches into the module global, so each case clears it
    first and restores the surrounding process state afterwards (belt-and-braces
    against cross-test pollution)."""

    def setUp(self):
        self._saved_workers = optimize._WORKERS
        self._saved_env = os.environ.get('OPTIMIZE_WORKERS')

    def tearDown(self):
        optimize._WORKERS = self._saved_workers
        if self._saved_env is None:
            os.environ.pop('OPTIMIZE_WORKERS', None)
        else:
            os.environ['OPTIMIZE_WORKERS'] = self._saved_env

    def test_resolves_from_env_var(self):
        optimize._WORKERS = None
        os.environ['OPTIMIZE_WORKERS'] = '3'
        self.assertEqual(optimize._resolve_workers(), 3)

    def test_default_leaves_one_core_free(self):
        optimize._WORKERS = None
        os.environ.pop('OPTIMIZE_WORKERS', None)
        with mock.patch.object(optimize.os, 'cpu_count', return_value=4):
            self.assertEqual(optimize._resolve_workers(), 3)

    def test_default_when_cpu_count_undeterminable(self):
        # DP#32: os.cpu_count() -> None must NOT crash and must NOT be swallowed
        # by a bare `or`; it falls back to 2 cores -> 1 worker (serial).
        optimize._WORKERS = None
        os.environ.pop('OPTIMIZE_WORKERS', None)
        with mock.patch.object(optimize.os, 'cpu_count', return_value=None):
            self.assertEqual(optimize._resolve_workers(), 1)


class TestSetWorkersResize(unittest.TestCase):
    """Resizing a LIVE pool tears the old one down so the next sweep rebuilds
    it at the new width (the shutdown-on-resize branch)."""

    def tearDown(self):
        optimize._shutdown_pool()
        optimize.set_workers(1)

    def test_resize_shuts_down_existing_pool(self):
        optimize.set_workers(2)
        pool = optimize._get_pool()
        self.assertIs(optimize._POOL, pool)
        # A different width with a live pool must shut the old one down.
        optimize.set_workers(3)
        self.assertIsNone(
            optimize._POOL,
            "resizing a live pool must shut it down so it rebuilds at the new width")


class TestScenarioRefusedSentinel(unittest.TestCase):
    """The two typed, EXPECTED infeasibilities (#891) cross the pool boundary as
    a ``_ScenarioRefused`` sentinel rather than aborting the sweep; any OTHER
    exception still propagates (fail loud, #657)."""

    def tearDown(self):
        optimize._shutdown_pool()
        optimize.set_workers(1)

    def test_worker_wraps_typed_refusal_in_sentinel(self):
        def _boom(**kwargs):
            raise ChargeLimitExceededError("charge 900000 exceeds limit 640000")
        with mock.patch.object(optimize, 'run_optimization', _boom):
            outcome = optimize._run_scenario_task(
                {'kwargs': {}, 'catch_refusals': True})
        self.assertIsInstance(outcome, optimize._ScenarioRefused)
        self.assertIn('ChargeLimitExceededError', outcome.reason)

    def test_worker_propagates_other_exceptions(self):
        def _boom(**kwargs):
            raise RuntimeError("a real bug must fail loud")
        with mock.patch.object(optimize, 'run_optimization', _boom):
            with self.assertRaises(RuntimeError):
                optimize._run_scenario_task({'kwargs': {}, 'catch_refusals': True})

    def test_sentinel_is_picklable(self):
        # It must survive the pool boundary (pickle round-trip) intact.
        s = optimize._ScenarioRefused("ChargeLimitExceededError: over")
        self.assertEqual(pickle.loads(pickle.dumps(s)), s)

    def test_refusal_survives_real_parallel_dispatch(self):
        # End-to-end: an over-limit declared refinance option (#891) refused
        # inside a worker comes back as a recorded refusal row -- the whole
        # parallel sweep completes rather than crashing.
        cfg = _refusal_cfg([
            {'id': 'ok', 'label': 'Modest cash-out', 'cash_out': 100000},
            {'id': 'over', 'label': 'Over-limit advance', 'cash_out': 500000},
        ])
        optimize.set_workers(2)
        try:
            results = optimize.run_ltv_exploration(cfg)
        finally:
            optimize._shutdown_pool()
            optimize.set_workers(1)
        refused = [r for r in results if r.get('refinance_refused')]
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0]['refinance_id'], 'over')
        self.assertIn('ChargeLimitExceededError', refused[0]['refinance_refusal'])
        # feasible rungs still scored -- one refusal did not sink the sweep
        self.assertTrue([r for r in results if not r.get('refinance_refused')])


if __name__ == '__main__':
    unittest.main()
