"""Parallel scenario sweep == serial sweep (perf/parallel-scenarios).

The optimizer evaluates each scenario as an INDEPENDENT pure fold (DP#26), so
the outer sweeps that loop ``run_optimization`` are dispatched across worker
processes. The HARD requirement is that parallelism is a pure speedup: the
collected results must be ranked in the EXACT same order, with the EXACT same
per-row values, as the serial (``--workers 1``) path -- so a parallel run's
report is byte-identical to a serial one. These tests lock that invariant and
that the serial fallback is a real, pool-free code path.

Issue #292: the pool now lives in ``process_pool`` (forkserver workers, one
pool at a time, a per-task liveness bound). Every parity test also asserts the
parallel leg REALLY dispatched through ``process_pool.map_ordered`` with more
than one scenario -- otherwise a regression that silently went serial (or a
test-session default of OPTIMIZE_WORKERS=1) would turn these into
serial-vs-serial comparisons that pass while proving nothing.
"""

from tax_data import default_tax_provider  # noqa: F401  (import-order parity)
import filecmp
import io
import contextlib
import json
import os
import pickle
import sys
import tempfile
import unittest
from concurrent.futures.process import BrokenProcessPool
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import optimize
import process_pool
from optimize import ChargeLimitExceededError
from test_optimize import _make_test_cfg, _write_cfg_to_file
from test_issue_891_refinance_refuse_and_skip import _cfg as _refusal_cfg
from test_input_contract import _load_example, _two_generation_subset


class _PoolSpy:
    """Spies on ``process_pool.map_ordered`` AND ``process_pool.get_pool``
    while still running the real pool. The second spy is what proves real
    out-of-process dispatch: a map_ordered that silently ran serially would
    still be called, but would never ask for a pool."""

    def __enter__(self):
        self._p1 = mock.patch.object(process_pool, 'map_ordered',
                                     wraps=process_pool.map_ordered)
        self._p2 = mock.patch.object(process_pool, 'get_pool',
                                     wraps=process_pool.get_pool)
        self.map_ordered = self._p1.start()
        self.get_pool = self._p2.start()
        return self

    def __exit__(self, *exc):
        self._p2.stop()
        self._p1.stop()
        return False


def _spy_map_ordered():
    return _PoolSpy()


#: Three declared income scenarios (the #665 fixture's shape). Without them the
#: test cfg has ONE auto-discovered income scenario, a single payload takes the
#: serial path, and an "income parity" test compares serial with serial.
_INCOME_SCENARIOS = [
    {"id": "stay", "label": "Stay at current job", "members": []},
    {"id": "salary_cut", "label": "Salary cut",
     "members": [{"role": "primary", "gross_income": 90000,
                  "kind": "employment", "from": "2026-01-01", "to": None}]},
    {"id": "job_loss", "label": "Job loss, EI only",
     "members": [{"role": "primary", "gross_income": 24000,
                  "kind": "ei", "from": "2026-01-01", "to": None}]},
]


def _cfg_with_income_scenarios():
    cfg = _make_test_cfg()
    cfg.setdefault('scenarios', {})['income'] = [dict(s) for s in _INCOME_SCENARIOS]
    return cfg


def _dispatched_in_parallel(spy) -> bool:
    """True when at least one batch of > 1 scenario went to map_ordered AND a
    real pool of more than one worker was requested for it."""
    return (any(len(c.args[1]) > 1 for c in spy.map_ordered.call_args_list)
            and any(c.args[0] > 1 for c in spy.get_pool.call_args_list))


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
        process_pool.shutdown_pool()
        optimize.set_workers(1)
        os.unlink(self.path)

    def test_ltv_exploration_parallel_matches_serial(self):
        optimize.set_workers(1)
        serial = optimize.explore('ltv', self.cfg, self.path)
        optimize.set_workers(4)
        with _spy_map_ordered() as spy:
            parallel = optimize.explore('ltv', self.cfg, self.path)
        self.assertTrue(_dispatched_in_parallel(spy),
                        "the parallel leg must really dispatch through the pool")
        self.assertEqual(_ranked_signature(serial), _ranked_signature(parallel),
                         "parallel LTV sweep must rank identically to serial")

    def test_income_scenario_exploration_parallel_matches_serial(self):
        os.unlink(self.path)
        self.cfg = _cfg_with_income_scenarios()
        self.path = _write_cfg_to_file(self.cfg)
        optimize.set_workers(1)
        serial = optimize.explore('income_scenario', self.cfg, self.path)
        optimize.set_workers(4)
        with _spy_map_ordered() as spy:
            parallel = optimize.explore('income_scenario', self.cfg, self.path)
        self.assertTrue(_dispatched_in_parallel(spy),
                        "the parallel leg must really dispatch through the pool")
        self.assertEqual(_ranked_signature(serial), _ranked_signature(parallel),
                         "parallel income sweep must rank identically to serial")


class TestSerialFallback(unittest.TestCase):
    """``--workers 1`` is a genuine pool-free path (provably == pre-parallel)."""

    def tearDown(self):
        process_pool.shutdown_pool()
        optimize.set_workers(1)

    def test_workers_one_opens_no_pool(self):
        cfg = _make_test_cfg()
        path = _write_cfg_to_file(cfg)
        try:
            process_pool.shutdown_pool()
            optimize.set_workers(1)
            optimize.explore('ltv', cfg, path)
            self.assertIsNone(
                process_pool._POOL,
                "workers=1 must take the serial path and never open a pool")
        finally:
            os.unlink(path)

    def test_map_scenarios_empty_is_noop(self):
        # Degenerate sweep: no scenarios -> no work, no pool, no error.
        optimize.set_workers(4)
        self.assertEqual(optimize._map_scenarios([]), [])
        self.assertIsNone(process_pool._POOL)

    def test_single_payload_and_in_worker_take_the_serial_path(self):
        optimize.set_workers(4)
        payload = {'label': 'only', 'kwargs': {}}
        with mock.patch.object(optimize, 'run_optimization', return_value=['r']):
            self.assertEqual(optimize._map_scenarios([payload]), [['r']])
            with mock.patch.object(process_pool, 'in_worker', return_value=True):
                self.assertEqual(optimize._map_scenarios([payload, payload]),
                                 [['r'], ['r']])
        self.assertIsNone(process_pool._POOL, "neither case may open a pool")


class TestCliWorkersFlag(unittest.TestCase):
    """The ``--workers`` CLI flag wires into ``set_workers`` in ``main()``.

    ``--list-objectives`` short-circuits ``main()`` before any input document is
    loaded, so this exercises the ``--workers -> set_workers`` handoff cheaply
    and deterministically (no contract, no optimizer pass, no pool)."""

    def tearDown(self):
        process_pool.shutdown_pool()
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

    def test_malformed_task_timeout_is_refused_even_when_serial(self):
        # #292: a typo in OPTIMIZE_TASK_TIMEOUT must not be silently ignored
        # just because this run never reaches the pool.
        argv = ['optimize.py', '--workers', '1', '--list-objectives']
        with mock.patch.dict(os.environ, {'OPTIMIZE_TASK_TIMEOUT': 'ten'}):
            with mock.patch.object(sys, 'argv', argv):
                with self.assertRaisesRegex(ValueError, 'OPTIMIZE_TASK_TIMEOUT'):
                    optimize.main()


class TestWorkerCountResolution(unittest.TestCase):
    """The --workers / OPTIMIZE_WORKERS / (available CPUs - 1) resolution ladder.

    #292: the default counts the CPUs this process may use
    (``process_pool.available_cpus``: the affinity mask), not the machine.

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
        with mock.patch.object(process_pool, 'available_cpus', return_value=4):
            self.assertEqual(optimize._resolve_workers(), 3)

    def test_default_when_cpu_count_undeterminable(self):
        # DP#32: an undeterminable count (None) must NOT crash and must NOT be
        # swallowed by a bare `or`; it resolves to 1 worker (serial).
        optimize._WORKERS = None
        os.environ.pop('OPTIMIZE_WORKERS', None)
        with mock.patch.object(process_pool, 'available_cpus', return_value=None):
            self.assertEqual(optimize._resolve_workers(), 1)

    def test_default_ignores_machine_cores_outside_the_affinity_mask(self):
        # The #292 measurement: 2 usable CPUs on a 16-core machine must give
        # 1 worker, not 15.
        optimize._WORKERS = None
        os.environ.pop('OPTIMIZE_WORKERS', None)
        fake_os = mock.Mock(spec=['sched_getaffinity', 'cpu_count'])
        fake_os.sched_getaffinity.return_value = {0, 1}
        fake_os.cpu_count.return_value = 16
        real = process_pool.available_cpus
        with mock.patch.object(process_pool, 'available_cpus',
                               side_effect=lambda: real(fake_os)):
            self.assertEqual(optimize._resolve_workers(), 1)

    def test_set_workers_beats_env(self):
        os.environ['OPTIMIZE_WORKERS'] = '3'
        optimize.set_workers(5)
        self.assertEqual(optimize._resolve_workers(), 5)


class TestSetWorkersResize(unittest.TestCase):
    """Resizing a LIVE pool tears the old one down so the next sweep rebuilds
    it at the new width (the shutdown-on-resize branch)."""

    def tearDown(self):
        process_pool.shutdown_pool()
        optimize.set_workers(1)

    def test_resize_shuts_down_existing_pool(self):
        optimize.set_workers(2)
        pool = process_pool.get_pool(optimize._resolve_workers())
        self.assertIs(process_pool._POOL, pool)
        # A different width with a live pool must shut the old one down.
        optimize.set_workers(3)
        self.assertIsNone(
            process_pool._POOL,
            "resizing a live pool must shut it down so it rebuilds at the new width")
        self.assertTrue(pool._shutdown_thread)
        rebuilt = process_pool.get_pool(optimize._resolve_workers())
        self.assertIsNot(rebuilt, pool)
        self.assertEqual(process_pool._POOL_WIDTH, 3)


class TestScenarioRefusedSentinel(unittest.TestCase):
    """The two typed, EXPECTED infeasibilities (#891) cross the pool boundary as
    a ``_ScenarioRefused`` sentinel rather than aborting the sweep; any OTHER
    exception still propagates (fail loud, #657)."""

    def tearDown(self):
        process_pool.shutdown_pool()
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
            results = optimize.explore('ltv', cfg)
        finally:
            process_pool.shutdown_pool()
            optimize.set_workers(1)
        refused = [r for r in results if r.get('refinance_refused')]
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0]['refinance_id'], 'over')
        self.assertIn('ChargeLimitExceededError', refused[0]['refinance_refusal'])
        # feasible rungs still scored -- one refusal did not sink the sweep
        self.assertTrue([r for r in results if not r.get('refinance_refused')])



class TestPayloadLabels(unittest.TestCase):
    """#292: every scenario payload names itself, so a stalled task is
    identified in the PoolStallError instead of hanging anonymously."""

    def tearDown(self):
        process_pool.shutdown_pool()
        optimize.set_workers(1)

    def _record_labels(self, dimension):
        calls = []

        def recorder(fn, items, labels, **kwargs):
            calls.append((list(items), list(labels)))
            return [fn(x) for x in items]

        cfg = _cfg_with_income_scenarios()
        path = _write_cfg_to_file(cfg)
        try:
            optimize.set_workers(2)
            with mock.patch.object(process_pool, 'map_ordered', side_effect=recorder):
                optimize.explore(dimension, cfg, path)
        finally:
            os.unlink(path)
        return calls

    def test_every_sweep_payload_carries_a_label(self):
        for dimension in ('ltv', 'income_scenario'):
            calls = self._record_labels(dimension)
            self.assertTrue(calls, f"{dimension}: the sweep never reached the pool")
            for items, labels in calls:
                self.assertEqual(labels, [p['label'] for p in items])
                self.assertTrue(all(isinstance(lab, str) and lab for lab in labels),
                                labels)
                self.assertEqual(len(set(labels)), len(labels),
                                 f"{dimension}: labels must be distinct: {labels}")

    def test_a_payload_without_a_label_is_refused(self):
        optimize.set_workers(2)
        with self.assertRaises(KeyError):
            optimize._map_scenarios([{'kwargs': {}}, {'kwargs': {}}])
        self.assertIsNone(process_pool._POOL)


class TestPoolFailuresPropagate(unittest.TestCase):
    """DP#32: a stalled or broken pool is never turned into a result."""

    def tearDown(self):
        process_pool.shutdown_pool()
        optimize.set_workers(1)

    def test_stall_and_broken_pool_propagate_from_map_scenarios(self):
        optimize.set_workers(2)
        payloads = [{'label': 'a', 'kwargs': {}}, {'label': 'b', 'kwargs': {}}]
        for exc in (process_pool.PoolStallError('stalled'),
                    BrokenProcessPool('worker died')):
            with mock.patch.object(process_pool, 'map_ordered', side_effect=exc):
                with self.assertRaises(type(exc)):
                    optimize._map_scenarios(payloads)


class TestCliJsonByteParity(unittest.TestCase):
    """``--workers 2`` writes a ``--json`` byte-identical to ``--workers 1``
    (DP#23), and the parallel run really used the pool."""

    def tearDown(self):
        process_pool.shutdown_pool()
        optimize.set_workers(1)

    def _main(self, argv):
        with mock.patch.object(sys, 'argv', argv):
            with contextlib.redirect_stdout(io.StringIO()):
                optimize.main()

    @pytest.mark.timeout(900)
    def test_workers2_json_byte_identical_to_serial(self):
        with tempfile.TemporaryDirectory() as d:
            contract = os.path.join(d, 'contract.json')
            with open(contract, 'w') as fh:
                json.dump(_two_generation_subset(_load_example()), fh)
            serial = os.path.join(d, 'w1.json')
            parallel = os.path.join(d, 'w2.json')
            self._main(['optimize.py', '--input', contract, '--workers', '1',
                        '--json', serial])
            with _spy_map_ordered() as spy:
                self._main(['optimize.py', '--input', contract, '--workers', '2',
                            '--json', parallel])
            self.assertTrue(_dispatched_in_parallel(spy),
                            "the --workers 2 run must really dispatch through the pool")
            self.assertTrue(filecmp.cmp(serial, parallel, shallow=False),
                            "--workers 2 --json must be byte-identical to --workers 1")


if __name__ == '__main__':
    unittest.main()
