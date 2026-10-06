from __future__ import annotations

import wave
from pathlib import Path

from src.services.tts import AudioResult, TTSError, VoiceSettings


class FakeTTSProvider:
    """Writes 0.1s of silence. `fail=True` simulates provider failure."""

    name = "fake"

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls: list[str] = []

    def synthesize(self, text: str, voice: VoiceSettings, out_path: Path) -> AudioResult:
        self.calls.append(text)
        if self.fail:
            raise TTSError("simulated tts failure")
        with wave.open(str(out_path), "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
            w.writeframes(b"\x00\x00" * 1600)
        return AudioResult(out_path, 16000, 0.1, self.name)
