"""Every process pool is built in ONE place, with an explicit non-fork start
method (issue #292).

Before #292, ``optimize.py`` and ``voi.py`` each built a bare
``ProcessPoolExecutor(max_workers=...)``. With no ``mp_context`` the workers
were ``fork``ed (Linux, Python < 3.14), and ``voi`` forked its pool while
optimize's persistent pool still had threads running in the parent -- the
fork-with-threads deadlock CPython 3.12 warns about. The fix routes both
through ``process_pool.get_pool``, which passes
``mp_context=multiprocessing.get_context(START_METHOD)``.

This guard keeps it that way: across every first-party source file
(``repo_scan.iter_source_files``, which includes ``tools/``), exactly one call
constructs a process pool, it lives in ``process_pool.py``, and it passes that
explicit context. A pool constructed anywhere else -- including through an
import alias -- fails here, naming the file and line.
"""
from __future__ import annotations

import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from repo_scan import ROOT, iter_source_files  # noqa: E402

import process_pool  # noqa: E402

#: Callables that construct a process pool, by their final name.
_POOL_CALLEES = {"ProcessPoolExecutor", "Pool"}
_ALLOWED_FILE = "process_pool.py"


def find_pool_constructions(src: str) -> list:
    """``[(lineno, ast.Call)]`` for every call in ``src`` that constructs a
    process pool: ``ProcessPoolExecutor(...)``, ``<mod>.ProcessPoolExecutor(...)``,
    ``multiprocessing.Pool(...)`` / ``<ctx>.Pool(...)``, and a bare name bound to
    either by ``from ... import X as Y``."""
    tree = ast.parse(src)
    aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name in _POOL_CALLEES:
                    aliases.add(a.asname if a.asname is not None else a.name)
    aliases.add("ProcessPoolExecutor")
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in aliases:
            found.append((node.lineno, node))
        elif isinstance(func, ast.Attribute) and func.attr in _POOL_CALLEES:
            found.append((node.lineno, node))
    return found


def _passes_explicit_context(call: ast.Call) -> bool:
    """True when ``call`` passes ``mp_context=multiprocessing.get_context(START_METHOD)``."""
    for kw in call.keywords:
        if kw.arg != "mp_context":
            continue
        v = kw.value
        return (isinstance(v, ast.Call)
                and isinstance(v.func, ast.Attribute) and v.func.attr == "get_context"
                and isinstance(v.func.value, ast.Name) and v.func.value.id == "multiprocessing"
                and len(v.args) == 1 and isinstance(v.args[0], ast.Name)
                and v.args[0].id == "START_METHOD")
    return False


def _read(relpath: str) -> str:
    with open(os.path.join(ROOT, relpath), encoding="utf-8") as fh:
        return fh.read()


def test_single_pool_construction_site_with_explicit_non_fork_context():
    sites = []
    for relpath in iter_source_files():
        for lineno, call in find_pool_constructions(_read(relpath)):
            sites.append((relpath, lineno, call))
    elsewhere = [f"{p}:{n}" for p, n, _ in sites if p != _ALLOWED_FILE]
    assert not elsewhere, (
        "a process pool is constructed outside process_pool.py -- route it through "
        f"process_pool.map_ordered/get_pool (DP#9, #292): {elsewhere}")
    assert len(sites) == 1, f"expected exactly one pool construction site, found {sites}"
    _, lineno, call = sites[0]
    assert _passes_explicit_context(call), (
        f"process_pool.py:{lineno} must pass "
        "mp_context=multiprocessing.get_context(START_METHOD)")


def test_no_fork_start_method_anywhere():
    offenders = []
    for relpath in iter_source_files():
        for node in ast.walk(ast.parse(_read(relpath))):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else (
                node.func.id if isinstance(node.func, ast.Name) else None)
            if name == "set_start_method":
                offenders.append(f"{relpath}:{node.lineno} set_start_method(...)")
            if name == "get_context" and any(
                    isinstance(a, ast.Constant) and a.value == "fork" for a in node.args):
                offenders.append(f"{relpath}:{node.lineno} get_context('fork')")
    assert not offenders, offenders
    assert process_pool.START_METHOD in {"forkserver", "spawn"}


def test_scanner_flags_a_bare_executor():
    assert len(find_pool_constructions("ProcessPoolExecutor(max_workers=2)")) == 1


def test_scanner_flags_an_aliased_import():
    src = ("from concurrent.futures import ProcessPoolExecutor as P\n"
           "P(max_workers=2)\n")
    assert len(find_pool_constructions(src)) == 1


def test_scanner_flags_a_module_attribute_and_multiprocessing_pool():
    src = ("import concurrent.futures as cf\n"
           "import multiprocessing\n"
           "cf.ProcessPoolExecutor(max_workers=2)\n"
           "multiprocessing.Pool(2)\n"
           "multiprocessing.get_context('spawn').Pool(2)\n")
    assert [n for n, _ in find_pool_constructions(src)] == [3, 4, 5]


def test_context_check_rejects_a_missing_or_literal_context():
    bare = find_pool_constructions("ProcessPoolExecutor(max_workers=2)")[0][1]
    literal = find_pool_constructions(
        "ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context('fork'))")[0][1]
    good = find_pool_constructions(
        "ProcessPoolExecutor(max_workers=2, "
        "mp_context=multiprocessing.get_context(START_METHOD))")[0][1]
    assert not _passes_explicit_context(bare)
    assert not _passes_explicit_context(literal)
    assert _passes_explicit_context(good)
