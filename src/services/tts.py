"""TTS abstraction. The rest of the app depends on TTSService only; Chatterbox lives in its own provider."""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

log = logging.getLogger("tts")


class TTSError(Exception):
    pass


@dataclass
class VoiceSettings:
    voice_id: str | None = None  # provider-specific (e.g. reference audio path)
    exaggeration: float = 0.5
    cfg_weight: float = 0.5
    speed: float = 1.0
    extra: dict = field(default_factory=dict)


@dataclass
class AudioResult:
    path: Path
    sample_rate: int
    duration_s: float | None = None
    provider: str = ""


class TTSProvider(Protocol):
    name: str

    def synthesize(self, text: str, voice: VoiceSettings, out_path: Path) -> AudioResult: ...


class NullTTSProvider:
    name = "null"

    def synthesize(self, text: str, voice: VoiceSettings, out_path: Path) -> AudioResult:
        raise TTSError("TTS is disabled (TTS_PROVIDER=null)")


class TTSService:
    """Runs synthesis off the event loop / request thread, with timeout and non-fatal failure semantics."""

    def __init__(self, provider: TTSProvider, output_dir: str | Path, timeout_s: float = 60.0,
                 default_voice: VoiceSettings | None = None, max_workers: int = 2):
        self.provider = provider
        self.out_dir = Path(output_dir)
        self.timeout_s = timeout_s
        self.default_voice = default_voice or VoiceSettings()
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="tts")

    def _target(self, key: str) -> Path:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        safe = "".join(c for c in key if c.isalnum() or c in "-_") or "audio"
        return self.out_dir / f"{safe}.wav"

    def synthesize(self, text: str, *, key: str, voice: VoiceSettings | None = None) -> AudioResult:
        """Blocking; raises TTSError on failure/timeout."""
        if not text.strip():
            raise TTSError("empty text")
        fut = self._pool.submit(self.provider.synthesize, text, voice or self.default_voice, self._target(key))
        try:
            return fut.result(timeout=self.timeout_s)
        except FutureTimeout as e:
            fut.cancel()
            raise TTSError(f"TTS timed out after {self.timeout_s}s") from e
        except TTSError:
            raise
        except Exception as e:  # provider bugs must not crash callers
            raise TTSError(f"TTS provider failure: {type(e).__name__}: {e}") from e

    def try_synthesize(self, text: str, *, key: str, voice: VoiceSettings | None = None) -> AudioResult | None:
        """Non-fatal variant: returns None (and logs) so the text path of the conversation keeps working."""
        try:
            return self.synthesize(text, key=key, voice=voice)
        except TTSError as e:
            log.warning("tts failed for %s: %s", key, e)
            return None

    async def synthesize_async(self, text: str, *, key: str, voice: VoiceSettings | None = None) -> AudioResult | None:
        return await asyncio.to_thread(self.try_synthesize, text, key=key, voice=voice)
