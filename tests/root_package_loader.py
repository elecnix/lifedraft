"""Load the repo-root package (`__init__.py`) deterministically, by absolute path.

`importlib.import_module("__init__")` resolves the bare name `__init__` against
`sys.path[0]`, which the test suite mutates heavily -- 150+ test modules do a
module-level `sys.path.insert(0, ...)` and never remove it. When a *subpackage*
directory whose own `__init__.py` declares no `__all__` (e.g. `countries/`)
reaches the front of `sys.path`, the bare import resolves to THAT package's
`__init__.py` instead of the repo root. The export tests then read the wrong file
and fail with `module '__init__' has no attribute '__all__'` -- an intermittent,
test-ordering-dependent red that surfaced once the parallel-scenario work (#951)
shifted execution order.

Loading the file by its explicit filesystem path is immune to `sys.path` ordering.
Because the spec points at the real file, `module.__file__` is the repo-root
`__init__.py`, so coverage.py still attributes its executed lines to that file --
these tests keep it off the zero-coverage list, which is why they exist.
"""

import importlib.util
import os

_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROOT_INIT = os.path.join(_ROOT_DIR, "__init__.py")


def load_root_package():
    """Return the repo-root package module, loaded from its absolute path."""
    spec = importlib.util.spec_from_file_location("_lifedraft_root_package", _ROOT_INIT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
