"""
core/voice.py — Continuous microphone capture with VAD (Voice Activity Detection).
Streams audio chunks when speech is detected, silences are used as utterance boundaries.
"""

import asyncio
import time
from typing import AsyncGenerator, Optional

try:
    import numpy as np
    _NUMPY_AVAILABLE = True
except ImportError:
    _NUMPY_AVAILABLE = False

from config.settings import Settings


class VoiceListener:
    """
    Async context manager that streams audio utterances from the microphone.
    Uses energy-based VAD to detect speech start/end.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.sample_rate = settings.voice_sample_rate
        self.silence_threshold = settings.voice_silence_threshold
        self.silence_duration = settings.voice_silence_duration
        self._stream = None
        self._audio = None

    async def __aenter__(self):
        # Lazy import so the app starts without pyaudio if not installed
        try:
            import pyaudio
            self._pa = pyaudio.PyAudio()
            self._stream = self._pa.open(
                format=pyaudio.paFloat32,
                channels=1,
                rate=self.sample_rate,
                input=True,
                frames_per_buffer=1024,
            )
            print("🎙️  Microphone opened.")
        except ImportError:
            print("⚠️  pyaudio not found. Install it: pip install pyaudio")
            self._stream = None
        return self

    async def __aexit__(self, *args):
        if self._stream:
            self._stream.stop_stream()
            self._stream.close()
            self._pa.terminate()

    async def stream(self) -> AsyncGenerator[bytes, None]:
        """
        Yields complete audio utterances as raw PCM bytes.
        An utterance is defined as: [speech frames] + [silence_duration seconds of silence].
        Falls back to keyboard input if microphone is unavailable.
        """
        if self._stream is None:
            async for chunk in self._keyboard_fallback():
                yield chunk
            return

        loop = asyncio.get_event_loop()
        recording = False
        frames = []
        silence_start: Optional[float] = None
        CHUNK = 1024

        print("👂 Listening... (speak to begin)")

        while True:
            # Read audio in executor to avoid blocking event loop
            data = await loop.run_in_executor(
                None, self._stream.read, CHUNK, False
            )
            samples = np.frombuffer(data, dtype=np.float32)
            energy = float(np.sqrt(np.mean(samples ** 2)))

            is_speech = energy > self.silence_threshold

            if is_speech:
                if not recording:
                    recording = True
                    frames = []
                    print("🔴 Recording...", end=" ", flush=True)
                frames.append(data)
                silence_start = None

            elif recording:
                frames.append(data)  # Include trailing silence
                if silence_start is None:
                    silence_start = time.time()
                elif time.time() - silence_start >= self.silence_duration:
                    print("✅")
                    recording = False
                    silence_start = None
                    yield b"".join(frames)
                    frames = []

            await asyncio.sleep(0)  # Yield control

    async def _keyboard_fallback(self) -> AsyncGenerator[bytes, None]:
        """Fallback: reads text from stdin and encodes as UTF-8 bytes for text-mode STT."""
        print("⌨️  Keyboard input mode (microphone unavailable)")
        loop = asyncio.get_event_loop()
        while True:
            try:
                text = await loop.run_in_executor(None, input, "\n💬 You: ")
                if text.strip():
                    # Encode text with a sentinel prefix so STT knows it's pre-transcribed
                    yield b"TEXT:" + text.encode("utf-8")
            except (EOFError, KeyboardInterrupt):
                break
