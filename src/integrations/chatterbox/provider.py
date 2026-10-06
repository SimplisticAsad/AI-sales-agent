"""Chatterbox TTS provider (Resemble AI). Local model or a self-hosted OpenAI-compatible HTTP server.

Chatterbox-specific code is confined to this file.
"""
from __future__ import annotations

import threading
import wave
from pathlib import Path

import httpx

from src.services.tts import AudioResult, TTSError, VoiceSettings


class ChatterboxProvider:
    name = "chatterbox"

    def __init__(self, mode: str = "local", device: str = "cuda", url: str = "", voice_prompt_path: str = "",
                 timeout: float = 60.0, client: httpx.Client | None = None):
        self.mode, self.device, self.url = mode, device, url.rstrip("/")
        self.voice_prompt_path = voice_prompt_path
        self._model = None
        self._lock = threading.Lock()
        self._client = client or httpx.Client(timeout=timeout)

    # -- local ----------------------------------------------------------------
    def _load(self):
        with self._lock:
            if self._model is None:
                try:
                    from chatterbox.tts import ChatterboxTTS  # heavy, optional dependency
                except ImportError as e:
                    raise TTSError("chatterbox-tts is not installed (pip install '.[chatterbox]')") from e
                self._model = ChatterboxTTS.from_pretrained(device=self.device)
        return self._model

    def _local(self, text: str, voice: VoiceSettings, out: Path) -> AudioResult:
        model = self._load()
        prompt = voice.voice_id or self.voice_prompt_path or None
        kwargs = {"exaggeration": voice.exaggeration, "cfg_weight": voice.cfg_weight}
        if prompt:
            kwargs["audio_prompt_path"] = prompt
        wav = model.generate(text, **kwargs)
        import torchaudio  # noqa: PLC0415

        torchaudio.save(str(out), wav, model.sr)
        return AudioResult(out, int(model.sr), wav.shape[-1] / model.sr, self.name)

    # -- http -----------------------------------------------------------------
    def _http(self, text: str, voice: VoiceSettings, out: Path) -> AudioResult:
        if not self.url:
            raise TTSError("CHATTERBOX_URL is not configured")
        try:
            r = self._client.post(
                f"{self.url}/v1/audio/speech",
                json={"input": text, "voice": voice.voice_id or "default", "response_format": "wav",
                      "exaggeration": voice.exaggeration, "cfg_weight": voice.cfg_weight, "speed": voice.speed},
            )
        except httpx.HTTPError as e:
            raise TTSError(f"chatterbox server unreachable: {type(e).__name__}") from e
        if r.status_code != 200 or not r.content:
            raise TTSError(f"chatterbox server error {r.status_code}")
        out.write_bytes(r.content)
        try:
            with wave.open(str(out)) as w:
                return AudioResult(out, w.getframerate(), w.getnframes() / w.getframerate(), self.name)
        except wave.Error:
            return AudioResult(out, 0, None, self.name)

    def synthesize(self, text: str, voice: VoiceSettings, out_path: Path) -> AudioResult:
        return self._http(text, voice, out_path) if self.mode == "http" else self._local(text, voice, out_path)
