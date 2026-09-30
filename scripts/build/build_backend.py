"""build_backend.py - the backend the installer carries: its own Python and code.

A student's PC has no Python, and a Python it has is not ours to fill. The
installer (desktop/, npm run dist) carries two folders this script makes:

  desktop/build/python    a relocatable CPython (python-build-standalone, the
                          same version as .venv) with requirements.txt in it
  desktop/build/backend   cygnus/, web/, config/ and .env.example

Installed, they land in resources/python and resources/backend, and the
desktop app runs resources/python/python.exe -m cygnus with the settings and
data in the user's own folder (desktop/main.js, cygnus/paths.py).

  .venv/Scripts/python scripts/build/build_backend.py          build both
  .venv/Scripts/python scripts/build/build_backend.py --code   only the code (fast)

Needs uv on PATH. Downloads Python once (~30 MB) and the wheels (~500 MB).
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BUILD = REPO / "desktop" / "build"
PYTHON_DIR = BUILD / "python"
BACKEND_DIR = BUILD / "backend"
CODE = ["cygnus", "web", "config"]
# The wake-word model (Apache 2.0; its README carries the licence), int8 only:
# the float copies and the test clips are not needed to spot "Cygnus".
KWS = "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
KWS_SRC = REPO / "data" / "models" / "kws" / KWS
KWS_KEEP = ["*.int8.onnx", "tokens.txt", "bpe.model", "cygnus_keywords.txt", "keywords.txt", "README.md"]
FILES = [".env.example", "requirements.txt"]
SKIP = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")


def _run(*cmd):
    print("  $", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True)


def build_python():
    """A standalone CPython of .venv's version, with the requirements in it."""
    version = "{}.{}.{}".format(*sys.version_info[:3])
    staging = BUILD / "_uv-python"
    shutil.rmtree(staging, ignore_errors=True)
    shutil.rmtree(PYTHON_DIR, ignore_errors=True)
    _run("uv", "python", "install", version, "--install-dir", staging, "--no-bin")
    # uv adds a cpython-3.14-... link beside cpython-3.14.6-...: the real one
    # carries the full version.
    found = [d for d in staging.iterdir()
             if d.name.startswith(f"cpython-{version}-") and (d / "python.exe").is_file()]
    if len(found) != 1:
        raise SystemExit(f"expected one Python in {staging}, found {found}")
    shutil.move(str(found[0]), PYTHON_DIR)
    shutil.rmtree(staging, ignore_errors=True)
    # uv marks its Pythons as managed so nothing installs into them by
    # accident; this copy is ours, and installing into it is the point.
    for marker in PYTHON_DIR.rglob("EXTERNALLY-MANAGED"):
        marker.unlink()
    python = PYTHON_DIR / "python.exe"
    _run("uv", "pip", "install", "--python", python, "--compile-bytecode", "-r", REPO / "requirements.txt")


def build_code():
    """The code the backend runs, without the repository around it."""
    shutil.rmtree(BACKEND_DIR, ignore_errors=True)
    BACKEND_DIR.mkdir(parents=True)
    for name in CODE:
        shutil.copytree(REPO / name, BACKEND_DIR / name, ignore=SKIP)
    for name in FILES:
        shutil.copy2(REPO / name, BACKEND_DIR / name)
    if not KWS_SRC.is_dir():
        raise SystemExit(f"the wake-word model is missing: {KWS_SRC} (see docs/SETUP.md)")
    target = BACKEND_DIR / "models" / "kws" / KWS
    target.mkdir(parents=True)
    for pattern in KWS_KEEP:
        for f in KWS_SRC.glob(pattern):
            shutil.copy2(f, target / f.name)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--code", action="store_true", help="only copy the code; keep the built Python")
    args = ap.parse_args(argv)
    BUILD.mkdir(parents=True, exist_ok=True)
    if not args.code or not (PYTHON_DIR / "python.exe").is_file():
        build_python()
    build_code()
    # The bundle on its own: its Python imports its code and the server app.
    subprocess.run([str(PYTHON_DIR / "python.exe"), "-c",
                    "import cygnus.config, cygnus.server.setup_routes; print('backend imports ok')"],
                   cwd=BACKEND_DIR, check=True)
    size = sum(f.stat().st_size for f in BUILD.rglob("*") if f.is_file()) / 2 ** 20
    print(f"built {BUILD} ({size:.0f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
