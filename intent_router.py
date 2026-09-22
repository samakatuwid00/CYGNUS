"""Moved to cygnus.brain.intent_router (docs/plans/RESTRUCTURE_PLAN.md, phase 2). This alias
keeps `import intent_router` working, as the same module object, until phase 3
rewrites the imports."""
import importlib as _importlib
import sys as _sys

_sys.modules[__name__] = _importlib.import_module("cygnus.brain.intent_router")
