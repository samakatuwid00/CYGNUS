"""Moved to cygnus.voice.transcript_pick (docs/plans/RESTRUCTURE_PLAN.md, phase 2). This alias
keeps `import transcript_pick` working, as the same module object, until phase 3
rewrites the imports."""
import importlib as _importlib
import sys as _sys

_sys.modules[__name__] = _importlib.import_module("cygnus.voice.transcript_pick")
