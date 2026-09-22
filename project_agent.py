"""Moved to cygnus.agents.project (docs/plans/RESTRUCTURE_PLAN.md, phase 2). This alias
keeps `import project_agent` working, as the same module object, until phase 3
rewrites the imports."""
import importlib as _importlib
import sys as _sys

_sys.modules[__name__] = _importlib.import_module("cygnus.agents.project")
