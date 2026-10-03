"""machine.py - what this PC has, and what it can run.

The first-run setup asks this before offering a local model or picking a
speech model: a student laptop with 8 GB of RAM and no graphics card is not
the author's machine, and a local model that cannot keep up is worse than
none (DISTRIBUTION_PLAN.md).

read() looks; recommend() decides, from the numbers alone, so it can be
tested without the hardware.
"""

from __future__ import annotations

import os
import shutil
import sys

# The display adapters' class key. HardwareInformation.qwMemorySize is the
# real memory of the card; WMI's AdapterRAM is a 32-bit number and reads 4 GB
# for anything larger.
_GPU_CLASS = r"SYSTEM\ControlSet001\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
_GB = 2 ** 30

# Where the lines fall. First estimates: the local ones are to be replaced
# by what scripts/bench/providers.py measures on real student machines.
# RAM as Windows reports it: a "16 GB" laptop shows about 15.2.
LOCAL_MIN_RAM_GB = 7.5        # below this, a 4B model and Cygnus do not fit together
# Only a graphics card makes a local model fast enough to be the main brain:
# on the author's 8-core CPU qwen3:4b ran at 14 tok/s and 28.9 s cold
# (config.py), and more RAM does not make a CPU faster.
LOCAL_MAIN_VRAM_GB = 6
WHISPER_TINY_BELOW_GB = 5.5
WHISPER_BASE_BELOW_GB = 11.5


def _gpus() -> list[dict]:
    """Display adapters with their memory in GB (None when not reported)."""
    if sys.platform != "win32":
        return []
    import winreg
    found = []
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _GPU_CLASS)
    except OSError:
        return []
    with root:
        index = 0
        while True:
            try:
                sub = winreg.EnumKey(root, index)
            except OSError:
                break
            index += 1
            try:
                with winreg.OpenKey(root, sub) as key:
                    name = winreg.QueryValueEx(key, "DriverDesc")[0]
                    try:
                        memory = round(winreg.QueryValueEx(key, "HardwareInformation.qwMemorySize")[0] / _GB, 1)
                    except OSError:
                        memory = None
            except OSError:
                continue
            found.append({"name": name, "vram_gb": memory})
    return found


def read(data_dir: str | None = None) -> dict:
    """RAM, cores, graphics cards and free disk on this PC."""
    import psutil

    from cygnus import paths
    where = data_dir or str(paths.DATA_DIR)
    os.makedirs(where, exist_ok=True)
    return {
        "ram_gb": round(psutil.virtual_memory().total / _GB, 1),
        "cores": psutil.cpu_count(logical=False) or os.cpu_count() or 1,
        "threads": os.cpu_count() or 1,
        "gpus": _gpus(),
        "disk_free_gb": round(shutil.disk_usage(where).free / _GB, 1),
    }


# What each model weighs on the first download, in MB: the model file on
# Hugging Face, measured in the author's cache 2026-10-03.
WHISPER_MB = {"tiny.en": 76, "base.en": 145, "small.en": 484, "medium.en": 1528}
_REPO_MB = {"minishlab/potion-multilingual-128M": 537}


def _have(repo: str, filename: str) -> bool:
    """True when the model is already in the Hugging Face cache. Looks only;
    never downloads."""
    try:
        from huggingface_hub import try_to_load_from_cache
        return isinstance(try_to_load_from_cache(repo, filename), str)
    except (ImportError, OSError, ValueError):     # no hub, unreadable cache, a bad repo id
        return False


def _whisper_repo(model: str) -> str:
    return model if "/" in model else f"Systran/faster-whisper-{model}"


def downloads(whisper: str) -> list[dict]:
    """What Cygnus fetches the first time it listens and routes, with the
    speech model `whisper`. Nothing said so before 2026-10-03, and on a new PC
    it is over a gigabyte."""
    from cygnus import config
    from cygnus.brain import semantic_route
    out, seen = [], set()
    for what, model in (("Speech", whisper), ("Wake word", config.WAKE_WHISPER_MODEL)):
        if model in seen:
            continue
        seen.add(model)
        out.append({"what": what, "model": model, "mb": WHISPER_MB.get(model),
                    "have": _have(_whisper_repo(model), "model.bin")})
    if semantic_route.MODE != "off":
        out.append({"what": "Command router", "model": semantic_route.MODEL,
                    "mb": _REPO_MB.get(semantic_route.MODEL),
                    "have": _have(semantic_route.MODEL, "model.safetensors")})
    return out


def recommend(m: dict) -> dict:
    """What to offer on a machine like `m` (read()'s shape)."""
    ram = m.get("ram_gb") or 0
    vram = max((g.get("vram_gb") or 0 for g in m.get("gpus") or []), default=0)
    if ram < LOCAL_MIN_RAM_GB:
        local, why = "none", f"{ram:g} GB of RAM is too little for a local model next to Cygnus"
    elif vram >= LOCAL_MAIN_VRAM_GB:
        local, why = "main", f"a graphics card with {vram:g} GB can run a local model as the main brain"
    else:
        local, why = "backup", "a local model fits, but runs slowly on the processor: keep it as the offline backup"
    if ram < WHISPER_TINY_BELOW_GB:
        whisper = "tiny.en"
    elif ram < WHISPER_BASE_BELOW_GB:
        whisper = "base.en"
    else:
        whisper = "small.en"
    return {"local": local, "local_why": why, "local_model": None if local == "none" else "qwen3:4b-instruct",
            "whisper": whisper, "cloud_first": local != "main"}
