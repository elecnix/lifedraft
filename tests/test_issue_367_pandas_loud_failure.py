"""Issue #367 (epic #603): pandas is an optional runtime dependency
(pyproject.toml declares it dev-only), used only by ``optimize.py`` to write
the flat ranked-summary CSVs requested via ``--export-csv``
(``strategy_results.csv`` / ``ltv_exploration_results.csv``).

Before this fix, when pandas was missing, ``--export-csv`` silently wrote
none of the two files it promises and printed no error, no warning — a user
on a non-dev install (``pip install .``) who asked for the CSV summary would
quietly get nothing and never learn the ranking file they expected doesn't
exist. Same failure class as the rest of epic #603: absence produces a
quieter, wrong-er result instead of an error.

The ranked-strategies *table* printed to stdout never needed pandas (it was
already plain Python in the fallback branch) and continues to work
identically with or without pandas — only the CSV *file* export is gated on
pandas, and it now fails loudly, naming the missing package and the install
command, exactly when ``--export-csv`` is actually requested.

DP#4/#15: fabricated round numbers, role-based names, no personal data.
"""
import json
import sys

import pytest

import optimize
import output_paths


def _cfg():
    """Minimal config that exercises both the strategy ranking and the LTV
    exploration path (property data present) through the real engine.

    Epic #603 Track C Phase 2b: ``optimize.py --input`` now reads a contract
    document (the sole wire format), validated by ``input_contract``, not
    the legacy ``family``/``property``/... dict directly -- so this fixture
    is now the shipped input-contract example (``schema/example.json``,
    which has a real house/mortgage/HELOC and a family income for the
    ranking + LTV-exploration paths this test exercises), trimmed to the
    one couple + children the legacy engine can represent, same as
    ``tests/test_input_contract.py``.
    """
    from test_input_contract import _load_example, _two_generation_subset
    return _two_generation_subset(_load_example())


@pytest.fixture
def no_pandas(monkeypatch):
    """Simulate pandas not being installed (non-dev install), without
    actually uninstalling it: setting sys.modules['pandas'] = None makes
    every subsequent `import pandas` raise ImportError, per the standard
    library import protocol."""
    monkeypatch.setitem(sys.modules, 'pandas', None)


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    """Redirect output_paths.output_path() into a tmp dir instead of the
    real ~/.cache/lifedraft (DP#15: never write test output into the
    user's real cache)."""
    d = tmp_path / "cache"
    monkeypatch.setattr(output_paths, 'CACHE_DIR', str(d))
    return d


@pytest.fixture
def input_json(tmp_path):
    path = tmp_path / "input.json"
    path.write_text(json.dumps(_cfg()))
    return str(path)


def test_export_csv_raises_loudly_without_pandas(no_pandas, cache_dir, input_json, monkeypatch, capsys):
    """The fix: --export-csv with pandas absent must fail loudly, naming
    pandas and the install command, instead of silently writing nothing."""
    monkeypatch.setattr(sys, 'argv', ['optimize.py', '--input', input_json, '--export-csv'])

    with pytest.raises(ModuleNotFoundError) as exc_info:
        optimize.main()

    message = str(exc_info.value)
    assert 'pandas' in message.lower()
    assert 'pip install' in message.lower()

    # And no half-written summary CSV is left behind either.
    assert not (cache_dir / "strategy_results.csv").exists()
    assert not (cache_dir / "ltv_exploration_results.csv").exists()


def test_ranked_table_and_other_exports_work_without_pandas(no_pandas, cache_dir, input_json, monkeypatch, capsys):
    """Without --export-csv, pandas is never touched: the ranked-strategies
    table still prints and the run completes normally (DP#8: the table was
    always plain Python)."""
    monkeypatch.setattr(sys, 'argv', ['optimize.py', '--input', input_json])

    optimize.main()  # must not raise

    out = capsys.readouterr().out
    assert 'RANKED STRATEGIES' in out
    # The table lists at least one discovered strategy with a dollar amount.
    assert '$' in out


def test_export_csv_writes_both_files_when_pandas_available(cache_dir, input_json, monkeypatch):
    """Sanity/regression check: when pandas *is* available, --export-csv
    still writes both the strategy summary and the LTV exploration CSV."""
    pytest.importorskip("pandas")
    monkeypatch.setattr(sys, 'argv', ['optimize.py', '--input', input_json, '--export-csv'])

    optimize.main()

    assert (cache_dir / "strategy_results.csv").exists()
    assert (cache_dir / "ltv_exploration_results.csv").exists()
