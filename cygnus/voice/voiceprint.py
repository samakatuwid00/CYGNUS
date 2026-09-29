"""voiceprint.py - only the owner's voice wakes Cygnus.

After the keyword spotter hears "Cygnus", the same audio window is turned into
a speaker embedding and compared with the owner's enrolled one (cosine
similarity). Below the threshold the wake is ignored and logged.

Model: 3D-Speaker ERes2Net, English (VoxCeleb), through sherpa-onnx. Chosen
2026-09-26 against CAM++, WeSpeaker ResNet34 and NeMo TitaNet-small on the
owner's wake set (48 wakes + the September test set) against 39 edge-tts
voices, with and without music: equal-error point about 0.23, owner rejected
1.1%, impostors accepted 1.4%; Cygnus's own voice never accepted. Measured
against synthetic voices only: a real person with a similar voice may score
higher. It is not security - a recording of the owner passes.

The voiceprint is a vector of numbers, not audio, stored in
data/voiceprint.json (git-ignored). scripts/voiceprint_enroll.py makes it;
delete the file to turn the check off.
"""

from __future__ import annotations

import json
import os
import threading
import time

import numpy as np

from cygnus import paths

MODEL = os.getenv("JARVIS_VOICEPRINT_MODEL") or str(
    paths.MODELS_DIR / "spk" / "3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx")
VOICEPRINT = paths.DATA_DIR / "voiceprint.json"
# Kept low on purpose: being ignored is worse than a rare stranger getting
# through. At 0.22 every owner clip in the wake set passed but one said over
# loud music (0.21); the highest synthetic impostor scored 0.29.
THRESHOLD = float(os.getenv("JARVIS_VOICEPRINT_THRESHOLD", "0.22"))
ENABLED = os.getenv("JARVIS_VOICEPRINT", "on").strip().lower() not in ("0", "off", "false", "no")
SR = 16000

_lock = threading.Lock()
_extractor = None
_enrolled = None          # (vector, mtime) of data/voiceprint.json


def trim(audio: np.ndarray, sr: int = SR, pad: float = 0.15) -> np.ndarray:
    """The speech in a clip: frames above a tenth of the loudest, padded.
    Silence in the embedding window dilutes the voice."""
    f = sr // 50
    n = len(audio) // f
    if n == 0:
        return audio
    e = np.sqrt((audio[:n * f].reshape(n, f).astype(np.float64) ** 2).mean(1))
    on = np.where(e > e.max() * 0.1)[0]
    if not on.size:
        return audio
    s = max(0, on[0] * f - int(pad * sr))
    t = min(len(audio), (on[-1] + 1) * f + int(pad * sr))
    return audio[s:t]


def _get_extractor():
    global _extractor
    if _extractor is None:
        import sherpa_onnx
        _extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=MODEL, num_threads=2))
    return _extractor


def embed(audio: np.ndarray, sr: int = SR) -> np.ndarray:
    """Unit-length speaker embedding of the speech in `audio`."""
    with _lock:
        ex = _get_extractor()
        st = ex.create_stream()
        st.accept_waveform(sr, trim(np.asarray(audio, dtype=np.float32), sr))
        st.input_finished()
        v = np.array(ex.compute(st), dtype=np.float64)
    return v / (np.linalg.norm(v) + 1e-12)


def enrolled() -> np.ndarray | None:
    """The owner's voiceprint, reread when the file changes; None if absent."""
    global _enrolled
    try:
        mtime = VOICEPRINT.stat().st_mtime
    except OSError:
        _enrolled = None
        return None
    if _enrolled is None or _enrolled[1] != mtime:
        try:
            v = np.array(json.loads(VOICEPRINT.read_text(encoding="utf-8"))["vector"], dtype=np.float64)
            _enrolled = (v / (np.linalg.norm(v) + 1e-12), mtime)
        except Exception:
            _enrolled = None
            return None
    return _enrolled[0]


def active() -> bool:
    """The check runs: on, a voiceprint enrolled, and the model present."""
    return ENABLED and enrolled() is not None and os.path.exists(MODEL)


def check(audio: np.ndarray, sr: int = SR) -> tuple[bool, float | None]:
    """(is it the owner, similarity). No voiceprint or model: (True, None)."""
    ref = enrolled()
    if not ENABLED or ref is None or not os.path.exists(MODEL):
        return True, None
    score = float(embed(audio, sr) @ ref)
    return score >= THRESHOLD, score


def enroll(clips: list[np.ndarray], sr: int = SR, source: str = "") -> dict:
    """Average the clips' embeddings into the voiceprint and save it."""
    if not clips:
        raise ValueError("no clips to enroll")
    vecs = [embed(c, sr) for c in clips]
    v = np.mean(vecs, axis=0)
    v = v / (np.linalg.norm(v) + 1e-12)
    spread = float(np.mean([x @ v for x in vecs]))
    record = {"model": os.path.basename(MODEL), "clips": len(clips), "source": source,
              "created": time.strftime("%Y-%m-%d %H:%M:%S"), "self_similarity": round(spread, 3),
              "vector": [round(float(x), 6) for x in v]}
    VOICEPRINT.parent.mkdir(parents=True, exist_ok=True)
    VOICEPRINT.write_text(json.dumps(record), encoding="utf-8")
    enrolled()
    return record
