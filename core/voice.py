"""
core/voice.py — Continuous microphone capture with VAD (Voice Activity Detection).
Streams transcribed text when speech is detected, using inline Whisper for STT.
Silences are used as utterance boundaries.
"""

import asyncio
import time
import numpy as np
from typing import AsyncGenerator, Optional
from config.settings import Settings


# Common Whisper hallucination phrases to filter out
_HALLUCINATION_PHRASES = [
    "thanks for watching",
    "thank you for watching",
    "please subscribe",
    "like and subscribe",
    "click the bell",
    "see you in the next video",
    "the transcript is empty",
    "you",
    "...",
    "",
]


class VoiceListener:
    """
    Async context manager that streams transcribed text utterances from the microphone.
    Uses energy-based VAD to detect speech start/end and Whisper for STT.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.sample_rate = settings.voice_sample_rate
        self.silence_threshold = settings.voice_silence_threshold
        self.silence_duration = settings.voice_silence_duration
        self._stream = None
        self._audio = None
        self._whisper_model = None
        self._load_whisper()

    def _load_whisper(self):
        """Load the Whisper model at init time so it's ready before listening starts."""
        try:
            import whisper
            model_name = self.settings.stt_model
            print(f"🔊 Loading Whisper model '{model_name}'...")
            self._whisper_model = whisper.load_model(model_name)
            print("✅ Whisper loaded.")
        except ImportError:
            print("⚠️  openai-whisper not installed. Run: pip install openai-whisper")
            print("   Voice input will fall back to keyboard mode.")

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

    async def stream(self) -> AsyncGenerator[str, None]:
        """
        Yields complete transcribed text utterances.
        An utterance is: [speech frames] + [silence_duration seconds of silence] → Whisper → text.
        Falls back to keyboard input if microphone is unavailable.
        """
        if self._stream is None:
            async for text in self._keyboard_fallback():
                yield text
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

                    # Transcribe the captured audio
                    raw_audio = b"".join(frames)
                    frames = []
                    transcript = await self._transcribe_chunk(raw_audio)
                    if transcript:
                        yield transcript

            await asyncio.sleep(0)  # Yield control

    async def _transcribe_chunk(self, raw_audio: bytes) -> Optional[str]:
        """Convert raw float32 PCM bytes to text using Whisper.
        Returns None if transcription fails or is a hallucination."""
        if self._whisper_model is None:
            return None

        loop = asyncio.get_event_loop()

        def _run():
            audio_np = np.frombuffer(raw_audio, dtype=np.float32)
            result = self._whisper_model.transcribe(
                audio_np,
                language=self.settings.stt_language,
                fp16=False,
            )
            return result["text"].strip()

        try:
            text = await loop.run_in_executor(None, _run)
        except Exception as e:
            print(f"  ⚠️  Whisper transcription error: {e}")
            return None

        # Filter hallucinations
        if self._is_hallucination(text):
            return None

        return text

    def _is_hallucination(self, text: str) -> bool:
        """Check if the text is a known Whisper hallucination."""
        cleaned = text.strip().lower().rstrip(".")
        return cleaned in _HALLUCINATION_PHRASES

    def has_wake_word(self, text: str) -> bool:
        """Check if the transcript contains a configured wake word."""
        text_lower = text.lower()
        return any(w in text_lower for w in self.settings.wake_words)

    def strip_wake_word(self, text: str) -> str:
        """Remove the wake word from the beginning of a transcript."""
        text_lower = text.lower()
        for word in self.settings.wake_words:
            if text_lower.startswith(word):
                return text[len(word):].strip().lstrip(",").strip()
        return text

    async def _keyboard_fallback(self) -> AsyncGenerator[str, None]:
        """Fallback: reads text from stdin as pre-transcribed text."""
        print("⌨️  Keyboard input mode (microphone unavailable)")
        loop = asyncio.get_event_loop()
        while True:
            try:
                text = await loop.run_in_executor(None, input, "\n💬 You: ")
                if text.strip():
                    yield text.strip()
            except (EOFError, KeyboardInterrupt):
                break
