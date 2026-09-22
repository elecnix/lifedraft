#!/usr/bin/env python3
"""Convert ``simulate_year_pure(...)`` call sites (issue #231, Slice 1).

The fold signature became ``simulate_year_pure(state, year, inputs)`` where
``inputs`` is a frozen ``YearInputs`` built by
``_build_year_inputs(allocations, config, **kwargs)``.

This rewrites each call to:

    simulate_year_pure(
        <state>,
        <year>,
        inputs=_build_year_inputs(
            <every original input argument, comments intact>
        ),
    )

Everything is sliced out of the ORIGINAL source by ``ast`` node positions
(``lineno``/``col_offset``/``end_lineno``/``end_col_offset``), so comments,
multi-line expressions and blank lines survive untouched -- the earlier
comma-splitting version this replaces did not.

Scaffolding: deleted before merge. Dry-run by default.

    python convert_year_pure_calls.py                     # dry run, all files
    python convert_year_pure_calls.py --apply FILE...     # write those files
    python convert_year_pure_calls.py --apply             # write every file
"""

import ast
import glob
import sys


def _offset(src: str, lineno: int, col: int) -> int:
    line_begin = 0
    for _ in range(lineno - 1):
        line_begin = src.index('\n', line_begin) + 1
    return line_begin + col


def _line_begin(src: str, lineno: int) -> int:
    line_begin = 0
    for _ in range(lineno - 1):
        line_begin = src.index('\n', line_begin) + 1
    return line_begin


def _leading_ws(text: str) -> str:
    ws = ''
    for ch in text:
        if ch in (' ', '\t'):
            ws += ch
        else:
            break
    return ws


def _indent_region(region: str, src_indent: str, target_indent: str) -> str:
    """Re-indent a block of argument lines from ``src_indent`` to ``target_indent``.

    Continuation lines keep their indentation RELATIVE to ``src_indent`` so a
    multi-line dict literal stays aligned.
    """
    lines = region.split('\n')
    out = []
    for i, ln in enumerate(lines):
        if not ln.strip():
            out.append('')
            continue
        body = ln.lstrip()
        here = ln[:len(ln) - len(body)]
        if i == 0:
            out.append(target_indent + body)
        elif here.startswith(src_indent):
            out.append(target_indent + here[len(src_indent):] + body)
        else:
            out.append(target_indent + body)
    return '\n'.join(out)


def transform_call(src: str, node: ast.Call) -> str:
    args = list(node.args) + list(node.keywords)
    if any(isinstance(k, ast.keyword) and k.arg is None for k in node.keywords):
        raise ValueError(f"line {node.lineno}: **kwargs splat is not supported")
    if len(args) < 4:
        raise ValueError(f"line {node.lineno}: expected >=4 args, got {len(args)}")

    call_start = _offset(src, node.lineno, node.col_offset)
    line_begin = _line_begin(src, node.lineno)
    before_call = src[line_begin:call_start]
    indent = _leading_ws(before_call)

    # state / year, sliced by AST position (multi-line expressions included)
    state_src = src[_offset(src, args[0].lineno, args[0].col_offset):
                    _offset(src, args[0].end_lineno, args[0].end_col_offset)]
    year_src = src[_offset(src, args[1].lineno, args[1].col_offset):
                   _offset(src, args[1].end_lineno, args[1].end_col_offset)]

    first_input = args[2]
    fi_start = _offset(src, first_input.lineno, first_input.col_offset)
    year_end = _offset(src, args[1].end_lineno, args[1].end_col_offset)
    close_paren = _offset(src, node.end_lineno, node.end_col_offset) - 1  # the ')'

    # Nothing may sit between state and year but a separator comma -- if a
    # comment did, this rewrite would silently drop it. Fail loudly instead.
    between = src[_offset(src, args[0].end_lineno, args[0].end_col_offset):
                   _offset(src, args[1].lineno, args[1].col_offset)]
    if between.strip(' \t\n,') != '':
        raise ValueError(f"line {node.lineno}: unexpected text between state and year: {between!r}")

    if first_input.lineno == args[1].lineno:
        # First input shares the year's line: take the rest of the call verbatim.
        year_comment = ''
        region = src[fi_start:close_paren].rstrip('\n \t')
        src_indent = indent + '    '
    else:
        # First input is on a later line: keep any trailing comment on the
        # year's own line attached to it, and take the region from the line
        # after (so blank lines and comments above the first input survive).
        year_line_end = src.find('\n', year_end)
        if year_line_end == -1:
            year_line_end = len(src)
        tail = src[year_end:year_line_end]
        year_comment = tail[tail.index('#'):].rstrip() if '#' in tail else ''
        region = src[year_line_end + 1:close_paren].strip('\n\t ')
        src_indent = _leading_ws(src[_line_begin(src, first_input.lineno):fi_start])

    body = _indent_region(region, src_indent, indent + '        ')

    out = [
        'simulate_year_pure(',
        indent + '    ' + state_src.strip() + ',',
        indent + '    ' + year_src.strip() + ',' + (('  ' + year_comment) if year_comment else ''),
        indent + '    inputs=_build_year_inputs(',
        body,
        indent + '    ),',
        indent + ')',
    ]
    return '\n'.join(out)


def add_import(src: str, tree: ast.AST) -> str:
    """Add ``_build_year_inputs`` to every simulation_state import of the fold.

    Only touches ``from simulation_state import ...`` statements that already
    name ``simulate_year_pure`` -- a module-level ``import simulation_state``
    needs nothing (the call sites use the bare name). Idempotent.
    """
    edits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.module != 'simulation_state':
            continue
        if '_build_year_inputs' in {a.name for a in node.names}:
            continue
        for a in node.names:
            if a.name != 'simulate_year_pure':
                continue
            start = _offset(src, a.lineno, a.col_offset)
            end = _offset(src, a.end_lineno, a.end_col_offset)
            edits.append((start, end, 'simulate_year_pure, _build_year_inputs'))
    out = src
    for start, end, rep in sorted(edits, key=lambda t: -t[0]):
        out = out[:start] + rep + out[end:]
    return out


def convert_file(path: str, dry_run: bool) -> int:
    src = open(path).read()
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == 'simulate_year_pure']
    if not calls:
        return 0

    replacements = []
    for node in calls:
        start = _offset(src, node.lineno, node.col_offset)
        end = _offset(src, node.end_lineno, node.end_col_offset)
        replacements.append((start, end, transform_call(src, node)))

    new_src = src
    for start, end, rep in sorted(replacements, key=lambda t: -t[0]):
        new_src = new_src[:start] + rep + new_src[end:]

    new_src = add_import(new_src, ast.parse(new_src))

    # Fail loudly rather than writing a file that no longer parses.
    ast.parse(new_src)

    if new_src != src:
        if dry_run:
            print(f"[dry-run] {path}: {len(calls)} call(s)")
        else:
            open(path, 'w').write(new_src)
            print(f"[ok] {path}: {len(calls)} call(s) converted")
    return len(calls)


def main(argv):
    dry_run = '--apply' not in argv
    explicit = [a for a in argv if not a.startswith('--')]
    if explicit:
        paths = explicit
    else:
        paths = sorted(glob.glob('tests/**/*.py', recursive=True)) + ['simulation.py']
    total = 0
    for path in paths:
        total += convert_file(path, dry_run=dry_run)
    print(f"\n{'DRY-RUN (no writes)' if dry_run else 'APPLIED'}: {total} call(s)")
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
