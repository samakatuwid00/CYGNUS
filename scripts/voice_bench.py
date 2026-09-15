"""Score wake-word and speech-to-text engines on the owner's voice test set.

    python scripts/voice_bench.py --kws-model DIR

Reads eval/voice_testset/ (scripts/voice_testset_record.py) and compares:
  wake:  today's listener (tiny.en + wake_engine's pattern over rolling
         2.5 s windows) vs sherpa-onnx keyword spotting on "Cygnus"
  STT:   today's command model (faster-whisper small.en, Cygnus's exact
         settings) vs NVIDIA Parakeet TDT through onnx-asr
Engines whose package or model is missing are skipped. Results also go to
eval/voice_testset/results.json.
"""

import argparse
import glob
import json
import os
import re
import sys
import time
import wave

import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
DATA = os.path.join(REPO, "eval", "voice_testset")
SR = 16000

# The words each command must come through with: the names Cygnus acts on.
KEY_TERMS = {
    "Open Docker": ["docker"], "Close Docker": ["docker"],
    "Open Visual Studio Code": ["visual studio code"], "Open Notepad and Chrome": ["notepad", "chrome"],
    "Open Telegram": ["telegram"], "What is the status?": ["status"],
    "Play Moira Dela Torre on Spotify": ["moira dela torre", "spotify"],
    "Search the web for the weather in Naga City": ["weather", "naga city"],
    "Open the iRIMS-V dashboard": ["irimsv", "dashboard"], "Switch to the full HUD view": ["full hud"],
    "Stop the music": ["stop", "music"], "Turn the volume up": ["volume"],
    "Open my Second Brain vault": ["second brain"], "Remember that I prefer lo-fi while coding": ["lofi"],
    "Open the Spotify app": ["spotify"], "What time is it?": ["time"], "Open the LRMIS report": ["lrmis"],
    "Close Visual Studio Code": ["visual studio code"], "Sige, go ahead": ["sige"],
    "Hindi, cancel that": ["hindi", "cancel"],
}


def load(path):
    with wave.open(path) as w:
        return np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768


def words(text):
    return re.sub(r"[^\w\s]", " ", (text or "").lower().replace("-", "")).split()


def wer(ref, hyp):
    r, h = words(ref), words(hyp)
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev, d[j] = d[j], cur
    return d[len(h)] / max(1, len(r))


def terms_right(expected, hyp):
    """Share of the command's key names that came through (spaces ignored)."""
    flat = "".join(words(hyp))
    terms = KEY_TERMS.get(expected, [])
    return sum(t.replace(" ", "") in flat for t in terms) / len(terms) if terms else 1.0


# ------------------------------------------------------------------ wake --

def whisper_wake_hits(audio, model):
    """Today's listener, replayed: 2.5 s windows every 1 s, its loudness gate,
    tiny.en, wake_engine's pattern, 4 s cooldown. Times of each wake."""
    from voice_engine import VAD_PARAMETERS, VoiceEngine
    from wake_engine import BURST_S, SILENCE_FLOOR, wake_word_in
    pad = np.zeros(SR, np.float32)
    audio = np.concatenate([pad, audio, pad])
    hits, last = [], -99.0
    for start in np.arange(0, max(0.0, len(audio) / SR - 2.5) + 0.001, 1.0):
        if start - last < 4.0:
            continue
        win = audio[int(start * SR): int((start + 2.5) * SR)]
        blk = int(SR * 0.1)
        n = len(win) // blk
        means = np.abs(win[:n * blk]).reshape(n, blk).mean(axis=1)
        k = max(1, min(n, int(round(BURST_S / 0.1))))
        if np.convolve(means, np.ones(k) / k, mode="valid").max() < SILENCE_FLOOR:
            continue
        segs, _ = model.transcribe(VoiceEngine._normalize(win), language="en", beam_size=1, vad_filter=True,
                                   vad_parameters=VAD_PARAMETERS, condition_on_previous_text=False)
        if wake_word_in(" ".join(s.text for s in segs)):
            hits.append(start)
            last = start
    return hits


def sherpa_spotter(model_dir, threshold, score):
    import sherpa_onnx
    pick = lambda part: sorted(glob.glob(os.path.join(model_dir, f"{part}*.int8.onnx"))
                               or glob.glob(os.path.join(model_dir, f"{part}*.onnx")))[0]
    tokens = os.path.join(model_dir, "tokens.txt")
    keywords = os.path.join(model_dir, "cygnus_keywords.txt")
    if not os.path.exists(keywords):
        # The model's own word pieces, one phrase a line, spelled the way the
        # word SOUNDS: as "CYGNUS" the spotter caught 0 of 20 of the owner's
        # clips, as "SIGNUS" 16 of 20 (2026-09-15). sentencepiece directly:
        # sherpa_onnx.text2token imports pypinyin even for English.
        import sentencepiece
        bpe = sentencepiece.SentencePieceProcessor(model_file=os.path.join(model_dir, "bpe.model"))
        with open(keywords, "w", encoding="utf-8") as f:
            for phrase in ("HEY SIGNUS", "SIGNUS"):
                f.write(" ".join(bpe.encode(phrase, out_type=str)) + f" @{phrase.replace(' ', '_')}\n")
    return sherpa_onnx.KeywordSpotter(tokens=tokens, encoder=pick("encoder"), decoder=pick("decoder"),
                                      joiner=pick("joiner"), num_threads=2, keywords_file=keywords,
                                      keywords_score=score, keywords_threshold=threshold, provider="cpu")


def sherpa_hits(audio, spotter):
    stream = spotter.create_stream()
    stream.accept_waveform(SR, np.concatenate([audio, np.zeros(int(0.8 * SR), np.float32)]))
    stream.input_finished()
    hits = []
    while spotter.is_ready(stream):
        spotter.decode_stream(stream)
        if spotter.get_result(stream):
            hits.append(1)
            spotter.reset_stream(stream)
    return hits


def score_wake(name, clips, detect):
    pos = [c for c in clips if c["kind"] == "wake"]
    neg = [c for c in clips if c["kind"] == "negative"]
    t0 = time.time()
    caught = [c["file"] for c in pos if detect(c["audio"])]
    false = sum(len(detect(c["audio"])) for c in neg)
    neg_min = sum(len(c["audio"]) for c in neg) / SR / 60
    row = {"engine": name, "caught": len(caught), "of": len(pos), "false_wakes": false,
           "negative_minutes": round(neg_min, 1), "missed": [c["file"] for c in pos if c["file"] not in caught],
           "seconds": round(time.time() - t0, 1)}
    print(f"  {name:34} caught {len(caught):>2}/{len(pos)}   false wakes {false} in {neg_min:.1f} min"
          f"   ({row['seconds']} s)")
    return row


# ------------------------------------------------------------------- STT --

def score_stt(name, clips, transcribe):
    rows = []
    for c in (c for c in clips if c["kind"] == "command"):
        t0 = time.time()
        hyp = transcribe(c)
        rows.append({"file": c["file"], "expected": c["expected"], "heard": hyp,
                     "wer": wer(c["expected"], hyp), "terms": terms_right(c["expected"], hyp),
                     "seconds": time.time() - t0})
    n = len(rows)
    summary = {"engine": name, "wer": sum(r["wer"] for r in rows) / n, "terms": sum(r["terms"] for r in rows) / n,
               "exact_terms": sum(r["terms"] == 1.0 for r in rows), "of": n,
               "sec_per_clip": sum(r["seconds"] for r in rows) / n, "clips": rows}
    print(f"  {name:34} word error {summary['wer']:.0%}   key names right {summary['terms']:.0%}"
          f"   all names right {summary['exact_terms']}/{n}   {summary['sec_per_clip']:.2f} s/clip")
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kws-model", help="sherpa-onnx keyword spotting model directory")
    ap.add_argument("--parakeet", default="nemo-parakeet-tdt-0.6b-v2", help="onnx-asr model name")
    args = ap.parse_args()
    manifest = json.load(open(os.path.join(DATA, "manifest.json"), encoding="utf-8"))
    clips = [dict(m, audio=load(os.path.join(DATA, m["file"]))) for m in manifest]
    print(f"{len(clips)} clips: " + ", ".join(f"{k} {sum(c['kind'] == k for c in clips)}"
                                              for k in ("wake", "command", "negative")))
    results = {"wake": [], "stt": []}

    print("\nWAKE WORD")
    from faster_whisper import WhisperModel
    tiny = WhisperModel("tiny.en", device="cpu", compute_type="int8", cpu_threads=2)
    results["wake"].append(score_wake("today: tiny.en + pattern", clips, lambda a: whisper_wake_hits(a, tiny)))
    if args.kws_model:
        try:
            for threshold, score in ((0.25, 1.0), (0.15, 1.5), (0.08, 2.0)):
                spotter = sherpa_spotter(args.kws_model, threshold, score)
                results["wake"].append(score_wake(f"sherpa-onnx kws (thr {threshold}, boost {score})",
                                                  clips, lambda a: sherpa_hits(a, spotter)))
        except Exception as e:
            print(f"  sherpa-onnx skipped: {type(e).__name__}: {e}")

    print("\nSPEECH-TO-TEXT (commands)")
    from config import WHISPER_INITIAL_PROMPT
    from voice_engine import VAD_PARAMETERS, VoiceEngine
    small = WhisperModel("small.en", device="cpu", compute_type="int8", cpu_threads=8)

    def whisper_text(c):
        segs, _ = small.transcribe(VoiceEngine._normalize(c["audio"]), language="en", beam_size=1,
                                   vad_filter=True, vad_parameters=VAD_PARAMETERS,
                                   initial_prompt=WHISPER_INITIAL_PROMPT, condition_on_previous_text=False)
        return " ".join(s.text for s in segs).strip()
    results["stt"].append(score_stt("today: faster-whisper small.en", clips, whisper_text))
    try:
        import onnx_asr
        parakeet = onnx_asr.load_model(args.parakeet, quantization="int8")
        results["stt"].append(score_stt(f"onnx-asr {args.parakeet} int8", clips,
                                        lambda c: str(parakeet.recognize(c["audio"], sample_rate=SR)).strip()))
    except Exception as e:
        print(f"  onnx-asr skipped: {type(e).__name__}: {e}")

    print("\nWHAT EACH ENGINE HEARD")
    for i, row in enumerate(results["stt"][0]["clips"]):
        print(f"  {row['expected']}")
        for eng in results["stt"]:
            print(f"      {eng['engine'][:28]:28} {eng['clips'][i]['heard']!r}")
    with open(os.path.join(DATA, "results.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, indent=1)


if __name__ == "__main__":
    main()
