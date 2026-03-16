"""
core/memory.py — Persistent memory and context management.
Stores interactions, projects, and preferences across sessions.
Supports semantic search via sentence-transformers (optional).
"""

import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from config.settings import Settings


@dataclass
class Interaction:
    timestamp: float
    command: str
    goal: str
    steps_count: int
    success: bool
    tags: list = field(default_factory=list)

    def to_context_string(self) -> str:
        dt = datetime.fromtimestamp(self.timestamp).strftime("%Y-%m-%d %H:%M")
        status = "✅" if self.success else "❌"
        return f"{status} [{dt}] {self.command} → {self.goal}"


@dataclass
class Project:
    name: str
    description: str
    created: float
    notes: list = field(default_factory=list)
    files: list = field(default_factory=list)


class MemoryManager:
    """
    Manages persistent memory for JARVIS.
    Storage structure:
      ~/.jarvis/memory/
        sessions.jsonl        — chronological interaction log
        projects.json         — named project registry
        preferences.json      — user preferences learned over time
        embeddings.pkl        — (optional) vector index for semantic search
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.memory_dir = Path(settings.memory_dir).expanduser()
        self.memory_dir.mkdir(parents=True, exist_ok=True)

        self._sessions_path = self.memory_dir / "sessions.jsonl"
        self._projects_path = self.memory_dir / "projects.json"
        self._prefs_path = self.memory_dir / "preferences.json"

        self._session_start: Optional[float] = None
        self._session_interactions: list[Interaction] = []

        # In-memory cache
        self._recent: list[Interaction] = self._load_recent(50)
        self._projects: dict[str, Project] = self._load_projects()
        self._prefs: dict[str, Any] = self._load_prefs()

    # ── Session Lifecycle ─────────────────────────────────────────────────────

    def log_session_start(self):
        self._session_start = time.time()
        self._session_interactions = []
        print(f"📒 Memory session started. {len(self._recent)} past interactions loaded.")

    def log_session_end(self):
        duration = time.time() - (self._session_start or time.time())
        print(f"📒 Session ended. Duration: {duration:.0f}s. "
              f"Interactions: {len(self._session_interactions)}.")

    # ── Interaction Storage ───────────────────────────────────────────────────

    def save_interaction(self, command: str, plan, results: list):
        """Persist a completed interaction to disk."""
        success = all(getattr(r, "success", True) for r in results)
        interaction = Interaction(
            timestamp=time.time(),
            command=command,
            goal=plan.goal,
            steps_count=len(plan.steps),
            success=success,
            tags=self._extract_tags(command),
        )
        self._session_interactions.append(interaction)
        self._recent.append(interaction)

        # Append to JSONL log
        with open(self._sessions_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(interaction)) + "\n")

    def _extract_tags(self, command: str) -> list[str]:
        """Simple keyword tagging."""
        tags = []
        keyword_map = {
            "file": ["file", "folder", "directory", "create", "delete", "move", "copy"],
            "code": ["code", "python", "script", "program", "function", "debug"],
            "browser": ["search", "google", "website", "browser", "url", "open"],
            "system": ["install", "update", "restart", "shutdown", "terminal", "command"],
            "email": ["email", "mail", "send", "gmail", "outlook"],
        }
        cmd_lower = command.lower()
        for tag, keywords in keyword_map.items():
            if any(kw in cmd_lower for kw in keywords):
                tags.append(tag)
        return tags

    # ── Context Retrieval ─────────────────────────────────────────────────────

    def get_recent_context(self, n: int = 10) -> list[str]:
        """Return the last N interactions as context strings."""
        recent = self._recent[-n:] if len(self._recent) > n else self._recent
        return [i.to_context_string() for i in recent]

    def search(self, query: str, top_k: int = 5) -> list[Interaction]:
        """Simple keyword search over recent interactions."""
        query_lower = query.lower()
        results = []
        for interaction in reversed(self._recent):
            if query_lower in interaction.command.lower() or query_lower in interaction.goal.lower():
                results.append(interaction)
            if len(results) >= top_k:
                break
        return results

    # ── Projects ──────────────────────────────────────────────────────────────

    def create_project(self, name: str, description: str = "") -> Project:
        project = Project(
            name=name,
            description=description,
            created=time.time(),
        )
        self._projects[name] = project
        self._save_projects()
        return project

    def get_project(self, name: str) -> Optional[Project]:
        return self._projects.get(name)

    def add_project_note(self, project_name: str, note: str):
        if project_name in self._projects:
            self._projects[project_name].notes.append(
                {"timestamp": time.time(), "note": note}
            )
            self._save_projects()

    def list_projects(self) -> list[str]:
        return list(self._projects.keys())

    # ── Preferences ───────────────────────────────────────────────────────────

    def set_preference(self, key: str, value: Any):
        self._prefs[key] = value
        self._save_prefs()

    def get_preference(self, key: str, default: Any = None) -> Any:
        return self._prefs.get(key, default)

    # ── Internal I/O ──────────────────────────────────────────────────────────

    def _load_recent(self, n: int) -> list[Interaction]:
        if not self._sessions_path.exists():
            return []
        interactions = []
        try:
            with open(self._sessions_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            data = json.loads(line)
                            interactions.append(Interaction(**data))
                        except Exception:
                            pass
        except Exception:
            pass
        return interactions[-n:]

    def _load_projects(self) -> dict[str, Project]:
        if not self._projects_path.exists():
            return {}
        try:
            with open(self._projects_path, encoding="utf-8") as f:
                data = json.load(f)
            return {k: Project(**v) for k, v in data.items()}
        except Exception:
            return {}

    def _save_projects(self):
        with open(self._projects_path, "w", encoding="utf-8") as f:
            json.dump({k: asdict(v) for k, v in self._projects.items()}, f, indent=2)

    def _load_prefs(self) -> dict:
        if not self._prefs_path.exists():
            return {}
        try:
            with open(self._prefs_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_prefs(self):
        with open(self._prefs_path, "w", encoding="utf-8") as f:
            json.dump(self._prefs, f, indent=2)
