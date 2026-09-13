#!/usr/bin/env python3
"""Classify every ``simulate_year_pure(...)`` call site in tests/ (issue #231).

Saves the classification so a future codemod for the YearInputs reshape can
reuse it: which call sites exist, whether the first four arguments are
positional or keyword, and whether any site breaks the "args 0-1 = state/year,
the rest are inputs material" assumption.

Run:  python callsite_classifier.py

Output: per-file counts, a flag per site (OK/multi-line/positional-alloc/none),
and a list of any problem sites (should be empty).
"""

import ast
import glob
import sys
from collections import Counter, defaultdict


def classify(src: str, path: str):
    tree = ast.parse(src)
    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, 'id', None) or getattr(func, 'attr', None)
        if name != 'simulate_year_pure':
            continue
        # is this the literal function (not a method on something)?
        if not isinstance(func, ast.Name):
            continue
        args = node.args
        kws = {k.arg: k for k in node.keywords if k.arg}
        npos = len(args)
        flags = []
        if npos >= 2:
            pass  # state/year positional
        if 'state' in kws:
            flags.append('kw:state')
        if 'year' in kws:
            flags.append('kw:year')
        # allocations/config forms
        if npos >= 4:
            flags.append('pos:alloc+cgf')
        elif 'allocations' in kws and 'config' in kws:
            flags.append('kw:alloc+cgf')
        elif 'allocations' in kws or 'config' in kws:
            flags.append('!Mixed allocs/config')
        else:
            flags.append('!missing alloc/config')
        # multiline?
        multiline = (getattr(node, 'end_lineno', node.lineno) or node.lineno) > node.lineno
        sites.append((node.lineno, 'multiline' if multiline else 'single', ','.join(flags)))
    return sites


def main():
    by_file = defaultdict(list)
    for path in sorted(glob.glob('tests/**/*.py', recursive=True)):
        try:
            src = open(path).read()
        except OSError:
            continue
        try:
            sites = classify(src, path)
        except SyntaxError:
            continue
        if sites:
            by_file[path] = sites

    total = 0
    flag_counts = Counter()
    multi = 0
    print('per-file call-site counts:')
    for path, sites in sorted(by_file.items()):
        total += len(sites)
        multi += sum(1 for _, kind, _ in sites if kind == 'multiline')
        for _, kind, flags in sites:
            flag_counts[flags] += 1
        print(f'  {path}: {len(sites)}')
    print(f'\nTOTAL sites: {total}  (multiline: {multi}, single-line: {total - multi})')
    print('\nshape flag counts:')
    for flags, n in flag_counts.most_common():
        print(f'  {flags}: {n}')


if __name__ == '__main__':
    sys.exit(main())