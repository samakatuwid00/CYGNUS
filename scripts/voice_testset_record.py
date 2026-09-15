"""Record the owner's voice test set for the wake word and speech-to-text.

Hands-free: each prompt shows on screen with a countdown, a high beep means
speak, a low beep means done. Nothing to press. Stop Cygnus first, or its wake
listener answers "Hey Cygnus" and its voice ends up in the clips.

    python scripts/voice_testset_record.py            # the whole set, ~7 min
    python scripts/voice_testset_record.py 14         # redo one clip by number

Clips and manifest.json go to eval/voice_testset/ (git-ignored: it is the
owner's voice).
"""

import ctypes
import json
import os
import subprocess
import sys
import time
import wave

import numpy as np
import sounddevice as sd

SR = 16000
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "eval", "voice_testset")

# (kind, what to say, seconds). "wake" clips must wake Cygnus; "command" clips
# are scored word by word; "negative" clips must never wake it.
PROMPTS = (
    [("wake", "Hey Cygnus", 4)] * 6
    + [("wake", "Cygnus", 4)] * 4
    + [("wake", "Hey Cygnus   (softly, as if from across the room)", 4)] * 3
    + [("wake", "Hey Cygnus, open Docker   (all in one breath)", 6)] * 3
    + [("wake", "Cygnus, what time is it?   (all in one breath)", 6)] * 2
    + [("wake", "Hey Cygnus   (fast and casual)", 4)] * 2
    + [("command", t, 7) for t in (
        "Open Docker",
        "Close Docker",
        "Open Visual Studio Code",
        "Open Notepad and Chrome",
        "Open Telegram",
        "What is the status?",
        "Play Moira Dela Torre on Spotify",
        "Search the web for the weather in Naga City",
        "Open the iRIMS-V dashboard",
        "Switch to the full HUD view",
        "Stop the music",
        "Turn the volume up",
        "Open my Second Brain vault",
        "Remember that I prefer lo-fi while coding",
        "Open the Spotify app",
        "What time is it?",
        "Open the LRMIS report",
        "Close Visual Studio Code",
        "Sige, go ahead",
        "Hindi, cancel that",
    )]
    + [("negative", "Talk normally for a minute about your day or your work. Do NOT say Cygnus.", 60),
       ("negative", "Read these slowly: signal, signals, design, sickness, sinus, significant, "
                    "sign this, seagull, cycles, sign us up, signing.", 25),
       ("negative", "Stay quiet. Let the room be, or play some music.", 40)]
)


def to_front():
    """Keep this window on top: the first session's window opened behind the
    others, and 40 of 43 clips caught only room tone (2026-09-13)."""
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        ctypes.windll.user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002)   # topmost, keep size/place
        ctypes.windll.user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def say(text):
    """Read a prompt aloud (Windows' own voice), so no one has to watch the window."""
    try:
        escaped = text.replace("'", "''")
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        "Add-Type -AssemblyName System.Speech; "
                        f"(New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak('{escaped}')"],
                       timeout=30, capture_output=True)
    except Exception:
        pass


def speech_seconds(audio):
    """Seconds of speech Silero hears in a clip; None when it cannot check."""
    try:
        from faster_whisper.vad import VadOptions, get_speech_timestamps
        stamps = get_speech_timestamps(audio, VadOptions(threshold=0.3, min_speech_duration_ms=150))
        return sum(s["end"] - s["start"] for s in stamps) / SR
    except Exception:
        return None


def beep(freq, ms=250):
    t = np.linspace(0, ms / 1000, int(44100 * ms / 1000), False)
    sd.play(0.3 * np.sin(2 * np.pi * freq * t), 44100)
    sd.wait()


def record(index, kind, text, seconds):
    to_front()
    print("\n" + "=" * 70)
    print(f"  {index + 1:>2}/{len(PROMPTS)}   [{kind.upper()}]")
    print(f"\n  SAY:  {text}\n")
    spoken = text.replace("   (", ", ").rstrip(")")
    say(("Say: " if kind != "negative" else "") + spoken + (". After the beep." if kind != "negative" else ""))
    for attempt in range(3):
        beep(880)
        print(f"  >>> SPEAK NOW ({seconds} s) <<<", flush=True)
        audio = sd.rec(int(SR * seconds), samplerate=SR, channels=1, dtype="float32")
        sd.wait()
        beep(440)
        heard = speech_seconds(audio[:, 0])
        if kind == "negative" or heard is None or heard >= 0.3 or attempt == 2:
            break
        print("  I didn't hear you. Once more, a little louder or closer.")
        say("I didn't hear you. Once more, after the beep.")
    if heard is not None and heard < 0.3 and kind != "negative":
        print("  (still no speech heard; keeping it anyway)")
    name = f"{index + 1:02d}_{kind}.wav"
    with wave.open(os.path.join(OUT, name), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(audio[:, 0], -1, 1) * 32767).astype(np.int16).tobytes())
    peak = float(np.abs(audio).max())
    print(f"  saved {name}   (loudness {peak:.2f}, speech {heard if heard is None else round(heard, 1)} s)")
    return {"file": name, "kind": kind, "expected": text.split("   (")[0], "seconds": seconds,
            "speech_seconds": heard}


def main():
    os.makedirs(OUT, exist_ok=True)
    manifest_path = os.path.join(OUT, "manifest.json")
    only = int(sys.argv[1]) - 1 if len(sys.argv) > 1 else None
    manifest = {}
    if os.path.exists(manifest_path):
        manifest = {m["file"]: m for m in json.load(open(manifest_path, encoding="utf-8"))}
    to_front()
    print("\n  CYGNUS VOICE TEST SET")
    print(f"  {len(PROMPTS)} clips, about 8 minutes. Speak the way you normally talk to Cygnus.")
    print("  Each prompt is read aloud. Speak AFTER the high beep; the low beep ends the clip.")
    print("  If it hears nothing, it asks you again. Nothing to press.")
    print(f"  Microphone: {sd.query_devices(kind='input')['name']}")
    say("Cygnus voice test. Speak after each high beep. Starting now.")
    time.sleep(1)
    for i, (kind, text, secs) in enumerate(PROMPTS):
        if only is not None and i != only:
            continue
        rec = record(i, kind, text, secs)
        manifest[rec["file"]] = rec
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(sorted(manifest.values(), key=lambda m: m["file"]), f, indent=1)
        time.sleep(0.8)
    print("\n" + "=" * 70)
    print("  DONE. Thank you. You can close this window.")
    time.sleep(8)


if __name__ == "__main__":
    main()
