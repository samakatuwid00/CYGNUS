"""The wake word through sherpa-onnx keyword spotting.

On the owner's 20 recorded wake clips (2026-09-15) Whisper tiny.en heard 15
in 185 s; this spotter heard 17 in 12 s with the same zero false wakes over
two minutes of ordinary talk and the confusable words (sinus, sign us up,
signal). The keyword must be spelled the way it SOUNDS: as "CYGNUS" the
spotter heard none, as "SIGNUS" 16 of 20. The word pieces come from the
model's own sentencepiece file.
"""

from __future__ import annotations

import glob
import os

import numpy as np

REPO = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DIR = os.path.join(REPO, "models", "kws", "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01")
# The bench's middle setting: 17/20 caught, 0 false wakes. Lower thresholds
# caught 18 with one false wake in a minute of talk.
THRESHOLD = float(os.getenv("JARVIS_WAKE_KWS_THRESHOLD", "0.15"))
SCORE = float(os.getenv("JARVIS_WAKE_KWS_SCORE", "1.5"))
PHRASES = ("HEY SIGNUS", "SIGNUS")


def _keywords_file(model_dir: str) -> str:
    path = os.path.join(model_dir, "cygnus_keywords.txt")
    if not os.path.exists(path):
        import sentencepiece
        bpe = sentencepiece.SentencePieceProcessor(model_file=os.path.join(model_dir, "bpe.model"))
        with open(path, "w", encoding="utf-8") as f:
            for phrase in PHRASES:
                f.write(" ".join(bpe.encode(phrase, out_type=str)) + f" @{phrase.replace(' ', '_')}\n")
    return path


class SherpaSpotter:
    """hears(window) -> the wake phrase as text ("hey cygnus"), or ''."""

    name = "sherpa"

    def __init__(self, model_dir: str = DEFAULT_DIR, threshold: float = THRESHOLD, score: float = SCORE,
                 threads: int = 2):
        import sherpa_onnx

        def pick(part):
            return sorted(glob.glob(os.path.join(model_dir, f"{part}*.int8.onnx"))
                          or glob.glob(os.path.join(model_dir, f"{part}*.onnx")))[0]

        self.spotter = sherpa_onnx.KeywordSpotter(
            tokens=os.path.join(model_dir, "tokens.txt"), encoder=pick("encoder"), decoder=pick("decoder"),
            joiner=pick("joiner"), num_threads=threads, keywords_file=_keywords_file(model_dir),
            keywords_score=score, keywords_threshold=threshold, provider="cpu")

    def hears(self, window: np.ndarray, sample_rate: int = 16000) -> str:
        stream = self.spotter.create_stream()
        # A little silence after the window lets the last piece be decoded.
        stream.accept_waveform(sample_rate, np.concatenate([window.astype(np.float32),
                                                            np.zeros(int(0.5 * sample_rate), np.float32)]))
        stream.input_finished()
        while self.spotter.is_ready(stream):
            self.spotter.decode_stream(stream)
            if self.spotter.get_result(stream):
                return "hey cygnus"
        return ""


def load(model_dir: str | None = None):
    """The spotter, or None with the reason printed (Cygnus then keeps Whisper)."""
    model_dir = model_dir or os.getenv("JARVIS_WAKE_KWS_DIR") or DEFAULT_DIR
    try:
        spotter = SherpaSpotter(model_dir)
        print(f"[WakeEngine] sherpa keyword spotter ready ({os.path.basename(model_dir)}, "
              f"threshold {THRESHOLD}, score {SCORE})", flush=True)
        return spotter
    except Exception as e:
        print(f"[WakeEngine] sherpa spotter unavailable ({type(e).__name__}: {e}); using Whisper", flush=True)
        return None
