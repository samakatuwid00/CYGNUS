"""Make, check or delete the owner's voiceprint (only that voice wakes Cygnus).

    python scripts/voiceprint_enroll.py              # enroll from eval/voice_wakeset/
    python scripts/voiceprint_enroll.py --check a.wav b.wav
    python scripts/voiceprint_enroll.py --delete     # turn the check off

Enrollment uses the wake set's normal-voice "Hey Cygnus" clips and the two
enrollment recordings (scripts/voice_testset_record.py --set wake), then scores
the wake set's other clips against the result so a weak voiceprint shows before
Cygnus relies on it. Restart Cygnus afterwards. The voiceprint is a list of
numbers in data/voiceprint.json, not audio; it never leaves this machine.
"""

import argparse
import json
import os
import sys
import urllib.request

import numpy as np
import soundfile as sf

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from cygnus import paths                     # noqa: E402
from cygnus.voice import voiceprint          # noqa: E402

WAKESET = paths.EVAL_DIR / "voice_wakeset"
MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
             + os.path.basename(voiceprint.MODEL))


def _load(path):
    a, sr = sf.read(str(path), dtype="float32")
    if a.ndim > 1:
        a = a[:, 0]
    if sr != voiceprint.SR:
        from scipy.signal import resample_poly
        a = resample_poly(a, voiceprint.SR, sr).astype(np.float32)
    return a


def ensure_model():
    if os.path.exists(voiceprint.MODEL):
        return
    print(f"Downloading the speaker model ({os.path.basename(voiceprint.MODEL)}, ~27 MB)...")
    os.makedirs(os.path.dirname(voiceprint.MODEL), exist_ok=True)
    urllib.request.urlretrieve(MODEL_URL, voiceprint.MODEL)


def enroll_from_wakeset():
    man = json.load(open(WAKESET / "manifest.json", encoding="utf-8"))
    use = [x for x in man if (x["kind"] == "wake" and x.get("condition") == "near"
                              and x["expected"] == "Hey Cygnus") or x["kind"] == "enroll"]
    if len(use) < 5:
        sys.exit("Not enough enrollment clips: run  python scripts/voice_testset_record.py --set wake  first.")
    rec = voiceprint.enroll([_load(WAKESET / x["file"]) for x in use], source="eval/voice_wakeset")
    print(f"Voiceprint saved to {voiceprint.VOICEPRINT} from {rec['clips']} clips "
          f"(agreement between them {rec['self_similarity']:.2f}).")
    used = {x["file"] for x in use}
    rest = [x for x in man if x["kind"] == "wake" and x["file"] not in used]
    if rest:
        scores = [(x.get("condition", ""), voiceprint.check(_load(WAKESET / x["file"]))[1]) for x in rest]
        low = [(c, s) for c, s in scores if s < voiceprint.THRESHOLD]
        vals = [s for _, s in scores]
        print(f"Your other {len(rest)} wake clips: median {np.median(vals):.2f}, lowest {min(vals):.2f} "
              f"(threshold {voiceprint.THRESHOLD:.2f}); {len(low)} would be ignored"
              + (f": {', '.join(f'{c} {s:.2f}' for c, s in low)}" if low else "."))
    print("Restart Cygnus to use it.")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", nargs="+", metavar="WAV", help="score clips against the voiceprint")
    ap.add_argument("--delete", action="store_true", help="delete the voiceprint (every voice wakes Cygnus)")
    a = ap.parse_args()
    if a.delete:
        if voiceprint.VOICEPRINT.exists():
            voiceprint.VOICEPRINT.unlink()
            print("Voiceprint deleted. Restart Cygnus: every voice wakes it again.")
        else:
            print("No voiceprint to delete.")
        return
    ensure_model()
    if a.check:
        if voiceprint.enrolled() is None:
            sys.exit("No voiceprint yet: run this script without --check first.")
        for f in a.check:
            ok, s = voiceprint.check(_load(f))
            print(f"{s:6.2f}  {'owner' if ok else 'NOT the owner'}  {f}")
        return
    enroll_from_wakeset()


if __name__ == "__main__":
    main()
