# Third-party software in Cygnus

Cygnus itself is under the PolyForm Noncommercial License 1.0.0
([LICENSE.md](LICENSE.md)). The installer also carries the software below,
each under its own licence, which Cygnus's licence does not change.

## Carried beside the Python packages

| What | Licence | Where its terms are |
|---|---|---|
| Electron and Chromium (the desktop app) | MIT; Chromium's components under their own | `LICENSE.electron.txt`, `LICENSES.chromium.html` in the app folder |
| Python 3.14.6 (python-build-standalone) | PSF License 2.0, with its bundled libraries' | `resources/python/LICENSE.txt` |
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

| aiohappyeyeballs | 2.7.1 | Python Software Foundation License |
| aiohttp | 3.14.3 | Apache-2.0 AND MIT |
| aiosignal | 1.4.0 | Apache Software License |
| annotated-doc | 0.0.5 | MIT |
| annotated-types | 0.8.0 | MIT |
| anyio | 4.15.1 | MIT |
| attrs | 26.1.0 | MIT |
| av | 19.0.0 | BSD-3-Clause |
| certifi | 2026.7.22 | Mozilla Public License 2.0 (MPL 2.0) |
| cffi | 2.1.1 | MIT-0 |
| charset-normalizer | 3.5.2 | MIT |
| click | 8.5.0 | BSD-3-Clause |
| cloudpickle | 3.1.2 | BSD License |
| colorama | 0.4.6 | BSD License |
| comtypes | 1.4.17 | MIT |
| cryptography | 50.0.1 | Apache-2.0 OR BSD-3-Clause |
| ctranslate2 | 4.8.2 | MIT |
| distro | 1.9.0 | Apache Software License |
| edge-tts | 7.2.8 | GNU Lesser General Public License v3 (LGPLv3) |
| fastapi | 0.142.2 | MIT |
| faster-whisper | 1.2.1 | MIT License |
| filelock | 4.0.7 | MIT |
| flatbuffers | 25.12.19 | Apache Software License |
| frozenlist | 1.8.0 | Apache-2.0 |
| fsspec | 2026.9.0 | BSD-3-Clause |
| google-auth | 2.59.0 | Apache Software License |
| google-genai | 2.25.0 | Apache-2.0 |
| greenlet | 3.5.6 | MIT AND PSF-2.0 |
| h11 | 0.16.0 | MIT License |
| hf-xet | 1.6.0 | Apache-2.0 |
| httpcore | 1.0.9 | BSD-3-Clause |
| httpcore2 | 2.13.1 | BSD-3-Clause |
| httpx | 0.28.1 | BSD License |
| httpx2 | 2.13.1 | BSD-3-Clause |
| huggingface_hub | 1.33.0 | Apache Software License |
| idna | 3.20 | BSD-3-Clause |
| jaraco.classes | 3.4.0 | MIT License |
| jaraco.context | 6.1.2 | MIT |
| jaraco.functools | 4.6.0 | MIT |
| Jinja2 | 3.1.6 | BSD License |
| jiter | 0.17.0 | MIT |
| joblib | 1.6.0 | BSD-3-Clause |
| keyring | 25.7.0 | MIT |
| markdown-it-py | 4.2.0 | MIT License |
| MarkupSafe | 3.0.3 | BSD-3-Clause |
| mdurl | 0.1.2 | MIT License |
| model2vec | 0.9.0 | MIT License |
| more-itertools | 11.1.0 | MIT |
| multidict | 6.9.1 | Apache License 2.0 |
| numpy | 2.5.3 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| onnxruntime | 1.30.0 | MIT License |
| openai | 3.22.1 | Apache-2.0 |
| opentelemetry-api | 1.45.0 | Apache-2.0 |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| pillow | 12.3.0 | MIT-CMU |
| pip | 26.2 | MIT |
| playwright | 1.63.0 | Apache-2.0 |
| propcache | 0.5.4 | Apache-2.0 |
| protobuf | 7.36.2 | 3-Clause BSD License |
| psutil | 7.2.2 | BSD-3-Clause |
| pyasn1 | 0.6.4 | BSD-2-Clause |
| pyasn1_modules | 0.4.2 | BSD License |
| pycaw | 20260927 | MIT |
| pycparser | 3.0 | BSD-3-Clause |
| pydantic | 2.13.5 | MIT |
| pydantic_core | 2.46.5 | MIT |
| pyee | 13.0.1 | MIT License |
| Pygments | 2.21.0 | BSD-2-Clause |
| python-dotenv | 1.2.3 | BSD-3-Clause |
| pywin32 | 312 | Python Software Foundation License |
| pywin32-ctypes | 0.2.3 | BSD-3-Clause |
| pywinauto | 0.6.9 | BSD License |
| PyYAML | 6.0.3 | MIT License |
| requests | 2.34.2 | Apache Software License |
| rich | 15.0.0 | MIT License |
| safetensors | 0.8.0 | Apache Software License |
| scipy | 1.18.1 | BSD License |
| sentencepiece | 0.2.2 | Apache-2.0 |
| sherpa-onnx-core | 1.13.8 | Apache-2.0 |
| sherpa_onnx | 1.13.8 | Apache licensed, as found in the LICENSE file |
| six | 1.17.0 | MIT License |
| sniffio | 1.3.1 | MIT License; Apache Software License |
| sounddevice | 0.5.6 | MIT |
| soundfile | 0.14.0 | BSD License |
| starlette | 1.7.0 | BSD-3-Clause |
| tabulate | 0.10.0 | MIT |
| tenacity | 9.1.4 | Apache Software License |
| tokenizers | 0.23.2 | Apache Software License |
| tqdm | 4.70.1 | MPL-2.0 AND MIT |
| truststore | 0.10.4 | MIT |
| typing-inspection | 0.4.4 | MIT |
| typing_extensions | 4.16.0 | PSF-2.0 |
| urllib3 | 2.8.0 | MIT |
| uvicorn | 0.54.0 | BSD-3-Clause |
| websockets | 16.1.1 | BSD-3-Clause |
| winrt-runtime | 3.2.1 | MIT |
| winrt-Windows.Foundation | 3.2.1 | MIT |
| winrt-Windows.Foundation.Collections | 3.2.1 | MIT |
| winrt-Windows.Globalization | 3.2.1 | MIT |
| winrt-Windows.Graphics.Imaging | 3.2.1 | MIT |
| winrt-Windows.Media.Ocr | 3.2.1 | MIT |
| winrt-Windows.Storage.Streams | 3.2.1 | MIT |
| yarl | 1.25.1 | Apache-2.0 |
