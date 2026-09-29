"""Record the owner's voice: the test set, or the wake-word set.

Hands-free: each prompt shows on screen and is read aloud, a high beep means
speak, a low beep means done. Nothing to press. Close Cygnus first, or its
wake listener answers "Hey Cygnus" and its voice ends up in the clips (the
script waits until it is closed).

    python scripts/voice_testset_record.py                 # the test set, ~8 min
    python scripts/voice_testset_record.py 14              # redo one test-set clip by number
    python scripts/voice_testset_record.py --set wake      # the wake-word set, ~12 min
    python scripts/voice_testset_record.py --set wake 23   # redo one wake-set clip

The test set (eval/voice_testset/) scores the wake word and speech-to-text.
The wake set (eval/voice_wakeset/) is for the custom wake model and for the
owner's voiceprint: "Cygnus" said near, far, soft, loud, fast, in one breath
with a command, and over music, plus enrollment speech and things that must
never wake Cygnus. While music plays, what the speakers play is recorded too
(<clip>_ref.wav), so echo cancellation can be tested on the real room.
Both folders are git-ignored: they are the owner's voice.
"""

import argparse
import ctypes
import json
import os
import socket
import subprocess
import threading
import time
import wave

import numpy as np
import sounddevice as sd

SR = 16000
EVAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "eval")

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

# The wake set: (kind, what to say, seconds, condition). Kinds beyond the test
# set's: "enroll" is speech for the voiceprint; "step" is an instruction with
# no recording (seconds = time given to do it); "music_on" / "music_off" wait
# until the speakers are (not) playing.
_PASSAGE = ("Read this in your normal voice: I work at the Department of Education in Naga City. "
            "Every morning I open my apps, check my projects, and play some music while I code. "
            "When I'm done, I close everything and review my notes for tomorrow.")
WAKE_PROMPTS = (
    [("music_off", "Stop any music for now: the first part needs a quiet room. I will wait until it is quiet.",
      60, "quiet"),
     ("step", "First part: sit where you usually talk to Cygnus, and use your normal voice.", 3, "near")]
    + [("wake", "Hey Cygnus", 4, "near")] * 8
    + [("wake", "Cygnus", 4, "near")] * 6
    + [("enroll", _PASSAGE, 25, "near"),
       ("enroll", "Talk freely for thirty seconds about anything, in your normal voice. Do NOT say Cygnus.", 30, "near")]
    + [("wake", "Hey Cygnus   (softly)", 4, "soft")] * 4
    + [("wake", "Hey Cygnus   (loudly, as if over music)", 4, "loud")] * 3
    + [("wake", "Hey Cygnus   (fast and casual)", 4, "fast")] * 3
    + [("wake", t + "   (all in one breath)", 6, "breath") for t in (
        "Hey Cygnus, open Spotify", "Cygnus, what time is it?", "Hey Cygnus, pause the music",
        "Cygnus, open Claude desktop", "Hey Cygnus, how many tabs are open?")]
    + [("step", "Now stand about two metres away from the laptop, facing it. You have ten seconds.", 10, "far")]
    + [("wake", "Hey Cygnus   (from where you are standing)", 4, "far")] * 5
    + [("step", "Come back to your usual spot.", 6, "near")]
    + [("music_on", "Start some music in Brave or Spotify at your normal listening volume. "
                    "I will wait until I hear it.", 90, "music")]
    + [("wake", "Hey Cygnus", 4, "music")] * 5
    + [("wake", "Cygnus", 4, "music")] * 3
    + [("step", "Now turn the music up loud, the way you like it. You have ten seconds.", 10, "music_loud")]
    + [("wake", "Hey Cygnus", 4, "music_loud")] * 5
    + [("wake", "Hey Cygnus, pause the music   (all in one breath)", 6, "music_loud")]
    + [("negative", "Stay quiet for thirty seconds and let the music play.", 30, "music_only")]
    + [("music_off", "Stop the music now. I will wait until it is quiet.", 60, "quiet")]
    + [("negative", "Read these slowly: signal, signals, design, sickness, sinus, significant, sign this, "
                    "seagull, cycles, sign us up, signing, Seamus, cygnet, census.", 25, "confusable"),
       ("negative", "Talk normally for one minute about your day or your work. Do NOT say Cygnus.", 60, "talk")]
)

SETS = {"test": ("voice_testset", PROMPTS), "wake": ("voice_wakeset", WAKE_PROMPTS)}


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
                       timeout=60, capture_output=True)
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


def _write(path, audio):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())


# ------------------------------------------------------------ speaker capture --

def _loopback():
    """What the speakers play, as a microphone (WASAPI loopback), or None."""
    try:
        import soundcard as sc
        spk = sc.default_speaker().name
        loops = [m for m in sc.all_microphones(include_loopback=True) if m.isloopback]
        return next((m for m in loops if m.name == spk), loops[0] if loops else None)
    except Exception:
        return None


def speaker_level(seconds=1.5):
    """RMS of what the speakers play right now; None when it cannot be read."""
    dev = _loopback()
    if dev is None:
        return None
    try:
        with dev.recorder(samplerate=SR, channels=1) as r:
            a = r.record(numframes=int(SR * seconds))[:, 0]
        return float(np.sqrt(np.mean(a ** 2)))
    except Exception:
        return None


MUSIC_RMS = 0.003       # the speakers are playing something


def wait_for_music(on, limit):
    """Poll the speakers until music is (not) playing; True when it is as asked."""
    end = time.time() + limit
    while time.time() < end:
        level = speaker_level()
        if level is None:
            print("  (can't read the speakers; carrying on)")
            time.sleep(8)
            return False
        print(f"  speakers: {level:.4f} {'(music)' if level >= MUSIC_RMS else '(quiet)'}", flush=True)
        if (level >= MUSIC_RMS) == on:
            return True
    return False


# ------------------------------------------------------------------ recording --

def record(index, total, out, kind, text, seconds, cond=None):
    to_front()
    print("\n" + "=" * 70)
    print(f"  {index + 1:>2}/{total}   [{kind.upper()}{' / ' + cond if cond else ''}]")
    print(f"\n  SAY:  {text}\n")
    spoken = text.replace("   (", ", ").rstrip(")")
    speaks = kind not in ("negative", "enroll")
    say(("Say: " if speaks else "") + spoken + (". After the beep." if speaks else ""))
    ref_dev = _loopback() if cond and cond.startswith("music") else None
    need = 5.0 if kind == "enroll" else 0.3
    for attempt in range(3):
        beep(880)
        print(f"  >>> SPEAK NOW ({seconds} s) <<<", flush=True)
        ref = {}
        audio = sd.rec(int(SR * seconds), samplerate=SR, channels=1, dtype="float32")
        if ref_dev is not None:
            def grab():
                try:
                    with ref_dev.recorder(samplerate=SR, channels=1) as r:
                        ref["a"] = r.record(numframes=int(SR * seconds))[:, 0]
                except Exception:
                    pass
            t = threading.Thread(target=grab)
            t.start()
            t.join()
        sd.wait()
        beep(440)
        heard = speech_seconds(audio[:, 0])
        if kind == "negative" or heard is None or heard >= need or attempt == 2:
            break
        print("  I didn't hear you. Once more, a little louder or closer.")
        say("I didn't hear you. Once more, after the beep.")
    if heard is not None and heard < need and kind != "negative":
        print("  (still not enough speech heard; keeping it anyway)")
    name = f"{index + 1:02d}_{kind}{'_' + cond if cond else ''}.wav"
    _write(os.path.join(out, name), audio[:, 0])
    entry = {"file": name, "kind": kind, "expected": text.split("   (")[0], "seconds": seconds,
             "speech_seconds": heard}
    if cond:
        entry["condition"] = cond
    if "a" in ref:
        entry["ref"] = name[:-4] + "_ref.wav"
        _write(os.path.join(out, entry["ref"]), ref["a"])
    peak = float(np.abs(audio).max())
    print(f"  saved {name}{' + speaker reference' if 'ref' in entry else ''}   "
          f"(loudness {peak:.2f}, speech {heard if heard is None else round(heard, 1)} s)")
    return entry


def cygnus_running(port=8765):
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_for_cygnus_closed():
    """Cygnus hears "Hey Cygnus" too and answers into the clips: wait until it
    is closed (tray icon > Quit). False if it is still running after 3 min."""
    if not cygnus_running():
        return True
    print("\n  Cygnus is running. Close it first (tray icon > Quit): it would wake on every")
    print("  'Hey Cygnus' and talk over the recording. Waiting...")
    say("Please close Cygnus first. I will start when it is closed.")
    end = time.time() + 180
    while time.time() < end:
        time.sleep(2)
        if not cygnus_running():
            print("  Cygnus is closed. Starting.")
            return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("clip", nargs="?", type=int, help="redo one clip by its number")
    ap.add_argument("--set", choices=sorted(SETS), default="test")
    ap.add_argument("--force", action="store_true", help="record even while Cygnus runs")
    a = ap.parse_args()
    folder, prompts = SETS[a.set]
    out = os.path.join(EVAL, folder)
    os.makedirs(out, exist_ok=True)
    manifest_path = os.path.join(out, "manifest.json")
    only = a.clip - 1 if a.clip else None
    manifest = {}
    if os.path.exists(manifest_path):
        manifest = {m["file"]: m for m in json.load(open(manifest_path, encoding="utf-8"))}
    to_front()
    clips = sum(p[0] not in ("step", "music_on", "music_off") for p in prompts)
    minutes = "8" if a.set == "test" else "12"
    print(f"\n  CYGNUS VOICE {'TEST SET' if a.set == 'test' else 'WAKE-WORD SET'}")
    print(f"  {clips} clips, about {minutes} minutes. Speak the way you normally talk to Cygnus.")
    print("  Each prompt is read aloud. Speak AFTER the high beep; the low beep ends the clip.")
    print("  If it hears nothing, it asks you again. Nothing to press.")
    print(f"  Microphone: {sd.query_devices(kind='input')['name']}")
    if not a.force and not wait_for_cygnus_closed():
        print("  Cygnus is still running. Close it and run this again (or pass --force).")
        return
    say("Cygnus voice recording. Speak after each high beep. Starting now.")
    time.sleep(1)
    for i, p in enumerate(prompts):
        kind, text, secs = p[:3]
        cond = p[3] if len(p) > 3 else None
        if only is not None and i != only:
            continue
        if kind in ("step", "music_on", "music_off"):
            if kind != "step":
                level = speaker_level()
                if level is not None and (level >= MUSIC_RMS) == (kind == "music_on"):
                    continue                # already so: nothing to ask
            to_front()
            print("\n" + "=" * 70 + f"\n  {text}\n")
            say(text)
            if kind == "step":
                time.sleep(secs)
            elif not wait_for_music(kind == "music_on", secs):
                print("  (didn't hear that change; carrying on)")
            continue
        rec = record(i, len(prompts), out, kind, text, secs, cond)
        manifest[rec["file"]] = rec
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(sorted(manifest.values(), key=lambda m: m["file"]), f, indent=1)
        time.sleep(0.8)
    print("\n" + "=" * 70)
    print("  DONE. Thank you. You can close this window.")
    say("Done. Thank you.")
    time.sleep(8)


if __name__ == "__main__":
    main()
