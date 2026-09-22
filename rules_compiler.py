"""Moved to cygnus.rules.compiler (docs/plans/RESTRUCTURE_PLAN.md, phase 2). This alias
keeps `import rules_compiler` working, as the same module object, until phase 3
rewrites the imports."""
import importlib as _importlib
import sys as _sys

_sys.modules[__name__] = _importlib.import_module("cygnus.rules.compiler")
