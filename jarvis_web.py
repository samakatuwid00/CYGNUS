"""Moved to cygnus.server.app (docs/plans/RESTRUCTURE_PLAN.md, phase 2).
`python jarvis_web.py` still starts the server (use `python -m cygnus`), and
`import jarvis_web` is the same module object, until phase 3."""
import importlib as _importlib
import sys as _sys

_app = _importlib.import_module("cygnus.server.app")
if __name__ == "__main__":
    _app.main()
else:
    _sys.modules[__name__] = _app
