"""Moved to cygnus.brain.context_assembler (docs/plans/RESTRUCTURE_PLAN.md, phase 2). This alias
keeps `import context_assembler` working, as the same module object, until phase 3
rewrites the imports."""
import importlib as _importlib
import sys as _sys

_sys.modules[__name__] = _importlib.import_module("cygnus.brain.context_assembler")
