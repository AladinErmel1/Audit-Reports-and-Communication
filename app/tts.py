"""Text-to-speech for narration, with graceful fallbacks.

Engines, in order of preference when TTS_ENGINE=auto:
  piper   offline neural voice (needs `pip install piper-tts` and PIPER_VOICE=/path/voice.onnx)
  edge    Microsoft Edge neural voices (needs `pip install edge-tts` and internet access)
  espeak  offline, robotic but always available once espeak-ng is installed
  silent  no narration; captions still carry the message

Every engine writes a mono 24 kHz WAV so the renderer can concatenate clips.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import wave
from pathlib import Path

SAMPLE_RATE = 24000


class TTSError(RuntimeError):
    pass


def _to_wav(src: Path, dst: Path) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-ac", "1", "-ar", str(SAMPLE_RATE), str(dst)],
                   check=True)


def _piper(text: str, out: Path) -> None:
    voice = os.environ.get("PIPER_VOICE")
    if not voice or not Path(voice).exists():
        raise TTSError("PIPER_VOICE is not set to a voice model")
    from piper import PiperVoice  # type: ignore

    raw = out.with_suffix(".piper.wav")
    v = PiperVoice.load(voice)
    with wave.open(str(raw), "wb") as w:
        v.synthesize_wav(text, w)
    _to_wav(raw, out)
    raw.unlink(missing_ok=True)


def _edge(text: str, out: Path) -> None:
    import edge_tts  # type: ignore

    voice = os.environ.get("EDGE_VOICE", "en-US-AndrewNeural")
    mp3 = out.with_suffix(".mp3")

    async def run() -> None:
        await edge_tts.Communicate(text, voice, rate=os.environ.get("EDGE_RATE", "+5%")).save(str(mp3))

    asyncio.run(run())
    if not mp3.exists() or mp3.stat().st_size == 0:
        raise TTSError("edge-tts returned no audio")
    _to_wav(mp3, out)
    mp3.unlink(missing_ok=True)


def _espeak(text: str, out: Path) -> None:
    exe = shutil.which("espeak-ng") or shutil.which("espeak")
    if not exe:
        raise TTSError("espeak-ng is not installed")
    raw = out.with_suffix(".espeak.wav")
    voice = os.environ.get("ESPEAK_VOICE", "en-us+m3")
    subprocess.run([exe, "-v", voice, "-s", os.environ.get("ESPEAK_SPEED", "165"), "-w", str(raw), text],
                   check=True, capture_output=True)
    _to_wav(raw, out)
    raw.unlink(missing_ok=True)


ENGINES = {"piper": _piper, "edge": _edge, "espeak": _espeak}


class Narrator:
    """Synthesises narration, remembering which engine works so failures cost only once."""

    def __init__(self, engine: str | None = None) -> None:
        choice = (engine or os.environ.get("TTS_ENGINE", "auto")).lower()
        self.order = ["piper", "edge", "espeak"] if choice == "auto" else [choice]
        self.used: str | None = None

    def synthesize(self, text: str, out: Path) -> float:
        """Write narration for `text` to `out` (WAV) and return its duration in seconds.

        Returns 0.0 when no engine is available (silent mode).
        """
        for name in list(self.order):
            if name == "silent":
                return 0.0
            try:
                ENGINES[name](text, out)
                self.used = name
                self.order = [name] + [n for n in self.order if n != name]
                return wav_duration(out)
            except Exception:  # noqa: BLE001 - any failure means "try the next engine"
                self.order.remove(name)
        self.used = "silent"
        return 0.0


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def silence(seconds: float) -> bytes:
    return b"\x00\x00" * int(SAMPLE_RATE * max(0.0, seconds))


def concat_wavs(parts: list[tuple[Path | None, float]], out: Path) -> None:
    """Concatenate clips; each entry is (wav or None, total seconds that slot must last)."""
    with wave.open(str(out), "wb") as dst:
        dst.setnchannels(1)
        dst.setsampwidth(2)
        dst.setframerate(SAMPLE_RATE)
        for wav_path, slot in parts:
            written = 0.0
            if wav_path is not None and wav_path.exists():
                with wave.open(str(wav_path), "rb") as src:
                    frames = src.readframes(src.getnframes())
                dst.writeframes(frames)
                written = len(frames) / 2 / SAMPLE_RATE
            dst.writeframes(silence(slot - written))
