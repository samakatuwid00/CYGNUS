"""third_party_notices.py - what other people's software the installer carries.

Run with the Python whose packages are listed - build_backend.py runs it with
the bundled one - so the list is what ships, not what someone remembers:

  desktop/build/python/python.exe scripts/build/third_party_notices.py > THIRD_PARTY_NOTICES.md

Each package's own licence text travels with it, in its .dist-info folder
under resources/python/Lib/site-packages, as those licences ask.
"""

import importlib.metadata as md
import sys

HEADER = """# Third-party software in Cygnus

Cygnus itself is under the PolyForm Noncommercial License 1.0.0
([LICENSE.md](LICENSE.md)). The installer also carries the software below,
each under its own licence, which Cygnus's licence does not change.

## Carried beside the Python packages

| What | Licence | Where its terms are |
|---|---|---|
| Electron and Chromium (the desktop app) | MIT; Chromium's components under their own | `LICENSE.electron.txt`, `LICENSES.chromium.html` in the app folder |
| Python {python} (python-build-standalone) | PSF License 2.0, with its bundled libraries' | `resources/python/LICENSE.txt` |
| Wake-word model: sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01 (k2-fsa / pkufool) | Apache 2.0 | `resources/backend/models/kws/.../README.md`; <https://www.modelscope.cn/pkufool/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01> |
| Lucide icons 0.460.0 (`web/vendor`) | ISC | the file's header; <https://lucide.dev/license> |

## edge-tts (LGPL 3.0)

The default voice uses edge-tts, under the GNU Lesser General Public
License 3.0. It is carried unchanged as its own package in
`resources/python/Lib/site-packages/edge_tts`, and you may replace it with
another version of your choosing. Its source: <https://github.com/rany2/edge-tts>.

## Python packages

| Package | Version | Licence |
|---|---|---|
"""


def _licence(meta) -> str:
    expression = meta.get("License-Expression")
    if expression:
        return expression
    classifiers = [c.split("::")[-1].strip() for c in (meta.get_all("Classifier") or [])
                   if c.startswith("License ::")]
    if classifiers:
        return "; ".join(classifiers)
    return ((meta.get("License") or "see the package").splitlines() or ["see the package"])[0][:60]


def main() -> int:
    python = "{}.{}.{}".format(*sys.version_info[:3])
    rows = sorted(((d.metadata["Name"], d.version, _licence(d.metadata)) for d in md.distributions()),
                  key=lambda r: r[0].lower())
    out = [HEADER.format(python=python)]
    out += [f"| {name} | {version} | {licence} |" for name, version, licence in rows]
    sys.stdout.write("\n".join(out) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
