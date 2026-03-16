"""
config/settings.py — Centralized configuration for JARVIS.
Edit config/settings.yaml to customize behavior.
"""

import os
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class Settings:
    # ── LLM ───────────────────────────────────────────────
    llm_provider: str = "anthropic"          # anthropic | openai | ollama
    llm_model: str = "claude-opus-4-5"       # Model name
    llm_api_key: str = ""                    # Loaded from env if blank
    llm_base_url: Optional[str] = None       # For Ollama: http://localhost:11434

    # ── STT ───────────────────────────────────────────────
    stt_provider: str = "whisper"            # whisper | google | deepgram
    stt_model: str = "base"                  # Whisper model size
    stt_language: str = "en"

    # ── TTS ───────────────────────────────────────────────
    tts_provider: str = "pyttsx3"            # pyttsx3 | elevenlabs | gtts
    tts_voice: str = "default"
    tts_rate: int = 175                      # Words per minute
    tts_elevenlabs_key: str = ""
    tts_elevenlabs_voice_id: str = ""

    # ── Voice / Wake Word ─────────────────────────────────
    require_wake_word: bool = True
    wake_words: list = field(default_factory=lambda: ["hey jarvis", "jarvis"])
    voice_sample_rate: int = 16000
    voice_silence_threshold: float = 0.01
    voice_silence_duration: float = 1.5      # Seconds of silence = end of utterance

    # ── Computer Control ──────────────────────────────────
    screenshot_interval: float = 1.0         # Seconds between feedback screenshots
    mouse_move_duration: float = 0.3         # Smooth mouse movement time
    typing_interval: float = 0.05            # Seconds between keystrokes
    ocr_language: str = "eng"

    # ── Vision Verification ───────────────────────────────
    vision_provider: str = "anthropic"       # anthropic (Claude Vision)
    vision_api_key: str = ""                 # Loaded from env ANTHROPIC_API_KEY
    vision_model: str = "claude-sonnet-4-20250514"
    vision_max_image_size: int = 1568        # Max px dimension for resized screenshots

    # ── Playwright Browser Automation ─────────────────────
    playwright_headless: bool = True         # Run Playwright browser headless
    playwright_timeout: int = 5000           # Element wait timeout in ms

    # ── Autonomous Loop ───────────────────────────────────
    autonomous_max_iterations: int = 15      # Safety cutoff for autonomous loop

    # ── Memory ────────────────────────────────────────────
    memory_dir: str = "~/.jarvis/memory"
    memory_max_context_items: int = 20
    memory_embedding_model: str = "all-MiniLM-L6-v2"  # For semantic search

    # ── Safety ────────────────────────────────────────────
    confirm_destructive: bool = True         # Ask before deleting files, etc.
    allowed_domains: list = field(default_factory=list)  # Empty = unrestricted
    blocked_paths: list = field(default_factory=lambda: [
        "/etc/passwd", "/etc/shadow", "~/.ssh"
    ])

    # ── Dashboard ─────────────────────────────────────────
    dashboard_port: int = 7860
    dashboard_host: str = "127.0.0.1"

    # ── Plugins / Tools ───────────────────────────────────
    enabled_tools: list = field(default_factory=lambda: [
        "mouse", "keyboard", "terminal", "browser",
        "files", "screenshot", "ocr", "search", "clipboard"
    ])

    @classmethod
    def load(cls, config_path: str = "config/settings.yaml") -> "Settings":
        """Load settings from YAML, then overlay environment variables."""
        s = cls()
        path = Path(config_path).expanduser()

        if path.exists():
            with open(path) as f:
                data = yaml.safe_load(f) or {}
            for k, v in data.items():
                if hasattr(s, k):
                    setattr(s, k, v)

        # Environment variable overrides
        env_map = {
            "ANTHROPIC_API_KEY": "llm_api_key",
            "OPENAI_API_KEY": "llm_api_key",
            "ELEVENLABS_API_KEY": "tts_elevenlabs_key",
        }
        for env_key, attr in env_map.items():
            val = os.getenv(env_key)
            if val:
                setattr(s, attr, val)

        # Auto-detect API key from env if not set
        if not s.llm_api_key:
            if s.llm_provider == "anthropic":
                s.llm_api_key = os.getenv("ANTHROPIC_API_KEY", "")
            elif s.llm_provider == "openai":
                s.llm_api_key = os.getenv("OPENAI_API_KEY", "")

        # Auto-detect vision API key from env if not set
        if not s.vision_api_key:
            s.vision_api_key = os.getenv("ANTHROPIC_API_KEY", "")

        return s

    def save(self, config_path: str = "config/settings.yaml"):
        """Persist current settings to YAML."""
        import dataclasses
        path = Path(config_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            yaml.dump(dataclasses.asdict(self), f, default_flow_style=False)
