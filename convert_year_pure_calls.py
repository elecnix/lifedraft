#!/usr/bin/env python3
"""Convert every ``simulate_year_pure(...)`` call site (issue #231, Slice 1).

The fold signature becomes ``simulate_year_pure(state, year, inputs)`` where
``inputs`` is a frozen ``YearInputs`` built by the private
``_build_year_inputs(allocations, config, **kwargs)``.

Every current call passes (state, year) as the FIRST TWO arguments (positional
or keyword) and everything else after them. The mechanical transform:

    simulate_year_pure(<state>, <year>, <allocations>, <config>, **kw)
        -> simulate_year_pure(<state>, <year>,
                              inputs=_build_year_inputs(<allocations>,
                                                        <config>, **kw))

The state/year arguments keep their original spelling (positional or keyword);
the remaining arguments are moved verbatim inside ``_build_year_inputs(...)``,
preserving comments and multi-line expressions.

This script is a one-shot migration aid (kept in the tree during review, then
deleted before merge). It rewrites files in place. Dry-run first.
"""

import ast
import glob
import sys


def _find_span(src: str, start: int) -> int:
    """Find the matching close paren for the '(' at src[start].

    Returns the index of the matching ')'. Skips strings and comments.
    """
    depth = 0
    i = start
    n = len(src)
    while i < n:
        c = src[i]
        if c == '#':
            while i < n and src[i] != '\n':
                i += 1
            continue
        if c in ('"', "'"):
            q = c
            i += 1
            while i < n:
                if src[i] == '\\':
                    i += 2
                    continue
                if src[i] == q:
                    # handle triple quotes
                    if src[i:i+3] == q*3:
                        i += 3
                        while i < n and src[i:i+3] != q*3:
                            if src[i] == '\\':
                                i += 2
                            else:
                                i += 1
                        if i < n:
                            i += 3
                    else:
                        i += 1
                    break
                i += 1
            continue
        if c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError(f"unbalanced parens from {start}")


def _split_top_level_commas(text: str) -> list:
    """Split text on COMMAS at bracket-depth 0, respecting strings/comments.

    Returns a list of (start, end, piece_text) slices into ``text``.
    """
    pieces = []
    depth = 0
    start = 0
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == '#':
            while i < n and text[i] != '\n':
                i += 1
            continue
        if c in ('"', "'"):
            q = c
            i += 1
            while i < n:
                if text[i] == '\\':
                    i += 2
                    continue
                if text[i] == q:
                    if text[i:i+3] == q*3:
                        i += 3
                        while i < n and text[i:i+3] != q*3:
                            i += 1
                        if i < n:
                            i += 3
                    else:
                        i += 1
                    break
                i += 1
            continue
        if c in '([{':
            depth += 1
        elif c in ')]}':
            depth -= 1
        elif c == ',' and depth == 0:
            pieces.append((start, i, text[start:i]))
            start = i + 1
        i += 1
    pieces.append((start, n, text[start:n]))
    return pieces


def _indent_piece(piece: str, base_indent: str) -> str:
    """Re-indent a possibly-multiline argument piece to ``base_indent``.

    The first line starts at base_indent; continuation lines are indented by
    base_indent + 4 spaces (matching how the surrounding call is written).
    """
    lines = piece.split('\n')
    out = []
    for ln in lines:
        stripped = ln.strip()
        if not stripped:
            out.append('')
            continue
        # remove common leading whitespace of the whole line set later; for
        # now just strip each line's own leading whitespace
        out.append(stripped)
    # first line gets base_indent; rest get base_indent + 4
    first = out[0]
    cont = [base_indent + '    ' + l for l in out[1:]]
    result = [base_indent + first] + cont
    return '\n'.join(result)


def transform_call(src: str, call_start: int, call_end: int) -> str:
    """Return the replacement text for one simulate_year_pure call.

    ``call_start`` points at 's' in 'simulate_year_pure'; ``call_end`` is the
    index AFTER the closing paren.
    """
    open_idx = src.index('(', call_start, call_end)
    close_idx = _find_span(src, open_idx)
    interior = src[open_idx + 1:close_idx]
    pieces = _split_top_level_commas(interior)
    # strip per-piece leading whitespace for analysis
    stripped_pieces = []
    for _, _, p in pieces:
        stripped_pieces.append(p.strip())
    if len(stripped_pieces) < 2:
        raise ValueError(f"call at {call_start} has <2 args: {interior!r}")

    first, second = stripped_pieces[0], stripped_pieces[1]
    # first two args must be state/year (positional or written state=/year=)
    first_kw = first.split('=', 1)[0].strip()
    second_kw = second.split('=', 1)[0].strip()
    if first_kw not in ('state', 'year') and second_kw not in ('state', 'year'):
        raise ValueError(f"call at {call_start} first-two args not state/year: "
                         f"{first!r} {second!r}")

    rest = pieces[2:]
    if not rest:
        raise ValueError(f"call at {call_start} has no input args after state/year")

    # Indentation base: leading whitespace of the line the call starts on
    line_start = src.rfind('\n', 0, call_start) + 1
    base = src[line_start:call_start]

    # State/year stay in the fold call; everything else moves into the builder.
    # Re-emit in a canonical multiline shape.
    first_line = base + 'simulate_year_pure('
    inner = '    inputs=_build_year_inputs('
    pieces_out = []
    for idx, (s, e, p) in enumerate(rest):
        pieces_out.append(_indent_piece(p, base + '        '))
    builder_body = ',\n'.join(pieces_out)

    state_line = base + '    ' + first
    year_line = base + '    ' + second

    out = [first_line, state_line + ',', year_line + ',', inner]
    for p in pieces_out:
        out.append(p + ',')
    out[-1] = out[-1][:-1]  # drop trailing comma on last builder arg
    out.append(base + '    ),')
    out.append(base + ')')
    return '\n'.join(out) + '\n'


def convert_file(path: str, dry_run: bool = True):
    with open(path) as f:
        src = f.read()
    tree = ast.parse(src)
    # find call nodes
    calls = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == 'simulate_year_pure'):
            # node position: (lineno, col_offset) .. (end_lineno, end_col_offset)
            calls.append((node, node.lineno, node.col_offset,
                          node.end_lineno, node.end_col_offset))
    if not calls:
        return 0
    lines = src.splitlines(keepends=True)
    off = {}
    pos = 0
    for i, ln in enumerate(lines, 1):
        off[i] = pos
        pos += len(ln)

    # sort by start, process in reverse so offsets stay valid
    ordered = []
    for node, sl, sc, el, ec in calls:
        start = off[sl] + sc
        end = off[el] + ec
        ordered.append((start, end))
    ordered.sort(key=lambda t: -t[0])

    new_src = src
    for start, end in ordered:
        replacement = transform_call(new_src, start, end)
        new_src = new_src[:start] + replacement + new_src[end:]
    if new_src != src:
        if dry_run:
            print(f"[dry-run] {path}: {len(ordered)} call(s) would change")
        else:
            with open(path, 'w') as f:
                f.write(new_src)
            print(f"[ok] {path}: {len(ordered)} call(s) converted")
    return len(ordered)


def main():
    dry_run = '--apply' not in sys.argv
    total = 0
    for path in sorted(glob.glob('tests/**/*.py', recursive=True)) + ['simulation.py']:
        total += convert_file(path, dry_run=dry_run)
    print(f"\n{'DRY-RUN (no writes)' if dry_run else 'APPLIED'}: {total} call(s)")


if __name__ == '__main__':
    main()