"""
core/stt.py — Speech-to-text transcription.
Supports OpenAI Whisper (local), Google Speech API, and Deepgram.
"""

import io
import numpy as np
from typing import Optional
from config.settings import Settings


class SpeechToText:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._model = None
        self._load_model()

    def _load_model(self):
        provider = self.settings.stt_provider
        if provider == "whisper":
            try:
                import whisper
                print(f"🔊 Loading Whisper model '{self.settings.stt_model}'...")
                self._model = whisper.load_model(self.settings.stt_model)
                print("✅ Whisper loaded.")
            except ImportError:
                print("⚠️  openai-whisper not installed. Run: pip install openai-whisper")
        elif provider == "google":
            try:
                import speech_recognition as sr
                self._recognizer = sr.Recognizer()
                print("✅ Google STT ready.")
            except ImportError:
                print("⚠️  SpeechRecognition not installed.")
        elif provider == "deepgram":
            try:
                from deepgram import Deepgram
                self._deepgram = Deepgram(self.settings.llm_api_key)
                print("✅ Deepgram ready.")
            except ImportError:
                print("⚠️  deepgram-sdk not installed.")

    async def transcribe(self, audio_bytes: bytes) -> Optional[str]:
        """
        Convert raw audio bytes (or TEXT: prefixed bytes) to a string transcript.
        Returns None if transcription fails or is empty.
        """
        # Keyboard fallback passthrough
        if audio_bytes.startswith(b"TEXT:"):
            return audio_bytes[5:].decode("utf-8").strip()

        provider = self.settings.stt_provider

        if provider == "whisper":
            return await self._whisper_transcribe(audio_bytes)
        elif provider == "google":
            return await self._google_transcribe(audio_bytes)
        elif provider == "deepgram":
            return await self._deepgram_transcribe(audio_bytes)

        return None

    async def _whisper_transcribe(self, audio_bytes: bytes) -> Optional[str]:
        if not self._model:
            return None
        import asyncio
        loop = asyncio.get_event_loop()

        def _run():
            # Convert raw float32 PCM to numpy array Whisper expects
            audio_np = np.frombuffer(audio_bytes, dtype=np.float32)
            result = self._model.transcribe(
                audio_np,
                language=self.settings.stt_language,
                fp16=False,
            )
            return result["text"].strip()

        return await loop.run_in_executor(None, _run)

    async def _google_transcribe(self, audio_bytes: bytes) -> Optional[str]:
        import speech_recognition as sr
        import asyncio
        loop = asyncio.get_event_loop()

        def _run():
            audio_data = sr.AudioData(
                audio_bytes,
                sample_rate=self.settings.voice_sample_rate,
                sample_width=2,
            )
            try:
                return self._recognizer.recognize_google(audio_data)
            except sr.UnknownValueError:
                return None

        return await loop.run_in_executor(None, _run)

    async def _deepgram_transcribe(self, audio_bytes: bytes) -> Optional[str]:
        response = await self._deepgram.transcription.prerecorded(
            {"buffer": audio_bytes, "mimetype": "audio/raw"},
            {"punctuate": True, "language": self.settings.stt_language},
        )
        try:
            return response["results"]["channels"][0]["alternatives"][0]["transcript"]
        except (KeyError, IndexError):
            return None

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
