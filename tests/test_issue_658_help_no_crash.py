#!/usr/bin/env python3
"""Issue #658: ``optimize.py --help`` crashed because a help string contained
a bare ``%`` that argparse's formatter tried to interpret as a format
specifier (``TypeError: not enough arguments for format string``). The tool's
own usage message was completely unreachable -- and ``--help`` is, for most
users, the first command they run.

## What this tests

1. The direct, fast-path guard (preferred per #658): the optimize CLI's
   parser is extracted into ``build_parser()`` so a test can call
   ``format_help()`` on it directly -- no subprocess. Pre-#658 this raised
   ``TypeError`` from argparse's ``%``-interpolation of the ``--preset``
   help string (``5%r/5.5%m``, ``7%/4.95%``, ``9%/3.5%``); the literal
   percents are now escaped as ``%%`` so they render as a single ``%``.
2. A parametrised subprocess smoke test across every CLI entry point that
   declares a parser (optimize, simulate, sensitivity, retirement_analysis,
   loop): ``--help`` must exit 0 with non-empty, traceback-free output. This
   is the recurrence guard #658 asks for -- a bare ``%`` slipped into any
   CLI's help string will fail this test, not ship.
"""

import subprocess
import sys
import unittest

from optimize import build_parser


# The CLI modules that declare an argparse parser and respond to --help.
# (make_retirement_input.py has no parser, so it is not listed.)
_CLI_MODULES = [
    'optimize.py',
    'simulate.py',
    'sensitivity.py',
    'retirement_analysis.py',
    'loop.py',
]


class TestOptimizeHelpRendersCleanly(unittest.TestCase):
    """The direct, fast-path guard (issue #658's preferred form): call the
    parser's ``format_help()`` directly -- no subprocess, no ``--help``
    exit-code ambiguity. Pre-#658 this raised ``TypeError``."""

    def test_format_help_does_not_crash(self):
        # Pre-#658: argparse's %-interpolation of the bare '5%r/5.5%m' etc.
        # in the --preset help string raised:
        #   TypeError: not enough arguments for format string
        help_text = build_parser().format_help()  # must not raise
        self.assertTrue(help_text, "format_help() must produce non-empty text")

    def test_literal_percents_render_as_single_percent(self):
        # The --preset help documents rates as percents. Escaped as '%%' in
        # the source, they must render as a single '%' in the help output --
        # not disappear, not double, and not crash the formatter.
        help_text = build_parser().format_help()
        self.assertIn('5%r/5.5%m', help_text,
                      "the escaped '%%' must render as a single '%' in the "
                      "--preset help line")
        self.assertIn('7%/4.95%', help_text)
        self.assertIn('9%/3.5%', help_text)

    def test_help_carries_the_program_description(self):
        help_text = build_parser().format_help()
        self.assertIn('Strategy Optimizer', help_text)
        self.assertIn('--preset', help_text)


class TestAllCliHelpExitsZero(unittest.TestCase):
    """Parametrised subprocess smoke test (issue #658's recurrence guard):
    every CLI entry point's ``--help`` must exit 0 with non-empty,
    traceback-free output. A bare ``%`` in any CLI's help string fails this
    test rather than shipping an unreachable usage message."""

    def _run_help(self, module: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, module, '--help'],
            capture_output=True, text=True, timeout=60,
        )

    def test_optimize_help_exits_zero(self):
        # The regression itself: optimize.py --help crashed pre-#658.
        proc = self._run_help('optimize.py')
        self.assertEqual(
            proc.returncode, 0,
            f"optimize.py --help must exit 0, not {proc.returncode}; "
            f"stderr:\n{proc.stderr}")
        self.assertTrue(proc.stdout, "--help must print non-empty output")
        self.assertNotIn('Traceback', proc.stderr)

    def test_every_cli_help_exits_zero(self):
        for module in _CLI_MODULES:
            with self.subTest(module=module):
                proc = self._run_help(module)
                self.assertEqual(
                    proc.returncode, 0,
                    f"{module} --help must exit 0, not {proc.returncode}; "
                    f"stderr:\n{proc.stderr}")
                self.assertTrue(proc.stdout, f"{module} --help printed nothing")
                self.assertNotIn('Traceback', proc.stderr)


if __name__ == '__main__':
    unittest.main()