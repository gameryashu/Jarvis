"""
core/llm.py — LLM reasoning and task planning.
Converts natural language commands into structured, executable action plans.
Supports Anthropic Claude, OpenAI GPT (and Groq), and local Ollama models.
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from config.settings import Settings
from core.memory import MemoryManager

logger = logging.getLogger(__name__)


# ── Data Models ───────────────────────────────────────────────────────────────

@dataclass
class ActionStep:
    """A single executable action in a plan."""
    tool: str                    # e.g. "terminal", "mouse_click", "type_text"
    params: dict = field(default_factory=dict)
    description: str = ""
    requires_confirmation: bool = False
    is_destructive: bool = False


@dataclass
class Plan:
    """A complete multi-step action plan."""
    goal: str
    steps: list[ActionStep]
    raw_response: str = ""

    def summary(self) -> str:
        lines = [f"Goal: {self.goal}", f"Steps ({len(self.steps)}):"]
        for i, s in enumerate(self.steps, 1):
            prefix = "⚠️" if s.is_destructive else "•"
            lines.append(f"  {prefix} {i}. [{s.tool}] {s.description}")
        return "\n".join(lines)


@dataclass
class RecoveryAction:
    """A corrective action when a step fails."""
    step: ActionStep
    reason: str


# ── System Prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are JARVIS, an expert AI system controller running on Windows 11.
User: yashu | Desktop: C:\\Users\\yashu\\Desktop | Default browser: Microsoft Edge

Your job is to translate a user's natural language command into a precise, executable action plan.

You have access to these tools:
- terminal: Run shell commands. params: {command: str}
- mouse_move: Move mouse to coordinates. params: {x: int, y: int}
- mouse_click: Click mouse. params: {x: int, y: int, button: "left"|"right"|"double"}
- mouse_scroll: Scroll. params: {x: int, y: int, direction: "up"|"down", amount: int}
- type_text: Type text. params: {text: str}
- key_press: Press keyboard shortcuts. params: {keys: str} e.g. "ctrl+c"
- open_app: Launch an application. params: {app: str, flags: str}
- screenshot: Take a screenshot. params: {region: null | [x,y,w,h]}
- ocr_read: Read text from screen region. params: {region: null | [x,y,w,h]}
- browser_open: Open URL in system default browser. params: {url: str}
- browser_search: Search the web in system browser. params: {query: str, engine: "google"|"duckduckgo"}
- play_youtube: Play a video on YouTube using Playwright. params: {query: str}
- play_spotify: Open Spotify and search. params: {query: str}
- file_read: Read a file. params: {path: str}
- file_write: Write to a file. params: {path: str, content: str, mode: "w"|"a"}
- file_delete: Delete a file/directory. params: {path: str}
- clipboard_copy: Copy text to clipboard. params: {text: str}
- clipboard_paste: Get current clipboard content. params: {}
- speak: Say something aloud. params: {text: str}
- wait: Pause execution. params: {seconds: float}
- notify: Show desktop notification. params: {title: str, message: str}

MEDIA RULES (CRITICAL — follow exactly):
- "play X" / "play X on youtube" / "watch X" / "put on X" → use play_youtube tool ONLY
- "play X on spotify" / "open X in spotify" → use play_spotify tool ONLY
- NEVER use browser_type, browser_click, or browser_search for YouTube or Spotify
- "open X website" / "go to X.com" → use browser_open with the URL
- For any music/video request: use dedicated media tools, not generic browser tools

APP RULES:
- "open calculator" → open_app with app: "calculator"
- "open notepad" → open_app with app: "notepad"
- Single words like "spotify", "discord", "chrome" → use open_app
- Creating folders: use terminal with mkdir command

GENERAL RULES:
1. Always respond with a JSON object — no markdown, no extra text.
2. Mark is_destructive: true for file_delete, file_write (overwrite), or terminal commands that modify system state.
3. Break complex tasks into small, verifiable steps.
4. If a task requires reading the screen first, add a screenshot/ocr step before clicking.
5. If the user's intent is ambiguous, pick the most likely interpretation and proceed.
6. Desktop path is C:\\Users\\yashu\\Desktop

RESPONSE FORMAT:
{
  "goal": "one-sentence description of what you're accomplishing",
  "steps": [
    {
      "tool": "tool_name",
      "params": { ... },
      "description": "human-readable description",
      "requires_confirmation": false,
      "is_destructive": false
    }
  ]
}
"""


# ── LLM Planner ───────────────────────────────────────────────────────────────

class LLMPlanner:
    def __init__(self, settings: Settings, memory: MemoryManager):
        self.settings = settings
        self.memory = memory
        self._client = self._init_client()

    def _init_client(self):
        provider = self.settings.llm_provider

        # Resolve API key and base URL from env vars first, then settings
        api_key = (
            os.environ.get("OPENAI_API_KEY")
            or os.environ.get("ANTHROPIC_API_KEY")
            or self.settings.llm_api_key
        )
        base_url = (
            os.environ.get("OPENAI_BASE_URL")
            or self.settings.llm_base_url
        )

        logger.info("LLM provider: %s | key prefix: %s | base_url: %s",
                    provider, (api_key or "")[:10], base_url)

        if provider == "anthropic":
            try:
                import anthropic
                return anthropic.Anthropic(api_key=api_key)
            except ImportError as e:
                raise ImportError("Install anthropic: pip install anthropic") from e

        elif provider == "openai":
            try:
                import openai
                kwargs: dict[str, Any] = {"api_key": api_key}
                if base_url:
                    kwargs["base_url"] = base_url
                return openai.OpenAI(**kwargs)
            except ImportError as e:
                raise ImportError("Install openai: pip install openai") from e

        elif provider == "ollama":
            try:
                import ollama
                return ollama
            except ImportError as e:
                raise ImportError("Install ollama: pip install ollama") from e

        else:
            raise ValueError(f"Unknown LLM provider: {provider}")

    def _build_context(self, command: str) -> str:
        """Inject recent memory context into the prompt."""
        recent = self.memory.get_recent_context(self.settings.memory_max_context_items)
        if not recent:
            return command
        ctx_str = "\n".join(f"- {item}" for item in recent[-5:])
        return f"Recent context:\n{ctx_str}\n\nCurrent command: {command}"

    async def plan(self, command: str) -> Plan:
        """Convert a natural language command into an executable Plan."""
        import asyncio
        prompt = self._build_context(command)
        loop = asyncio.get_event_loop()

        raw = await loop.run_in_executor(None, self._call_llm, prompt)
        return self._parse_plan(command, raw)

    def _call_llm(self, prompt: str) -> str:
        provider = self.settings.llm_provider

        if provider == "anthropic":
            response = self._client.messages.create(
                model=self.settings.llm_model,
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text

        elif provider == "openai":
            response = self._client.chat.completions.create(
                model=self.settings.llm_model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
            )
            return response.choices[0].message.content

        elif provider == "ollama":
            import ollama
            response = ollama.chat(
                model=self.settings.llm_model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            )
            return response["message"]["content"]

        return "{}"

    def _parse_plan(self, original_command: str, raw: str) -> Plan:
        """Parse LLM JSON response into a Plan object."""
        raw = re.sub(r"```json\s*|\s*```", "", raw).strip()

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group())
                except json.JSONDecodeError:
                    data = None
            else:
                data = None

        if data is None:
            return Plan(
                goal=original_command,
                steps=[ActionStep(
                    tool="speak",
                    params={"text": f"I wasn't sure how to do that: {raw[:200]}"},
                    description="Report uncertainty",
                )],
                raw_response=raw,
            )

        steps = []
        for s in data.get("steps", []):
            steps.append(ActionStep(
                tool=s.get("tool", "speak"),
                params=s.get("params", {}),
                description=s.get("description", ""),
                requires_confirmation=s.get("requires_confirmation", False),
                is_destructive=s.get("is_destructive", False),
            ))

        return Plan(
            goal=data.get("goal", original_command),
            steps=steps,
            raw_response=raw,
        )

    async def recover(self, failed_step: ActionStep, verification) -> Optional[ActionStep]:
        """Ask the LLM for a recovery action when a step fails."""
        import asyncio
        prompt = (
            f"A step failed.\n"
            f"Step: {failed_step.description}\n"
            f"Tool: {failed_step.tool}, Params: {failed_step.params}\n"
            f"Failure reason: {verification.reason}\n\n"
            f"Suggest ONE recovery action in the same JSON step format."
        )
        loop = asyncio.get_event_loop()
        raw = await loop.run_in_executor(None, self._call_llm, prompt)
        try:
            data = json.loads(raw)
            if "steps" in data and data["steps"]:
                s = data["steps"][0]
            else:
                s = data
            return ActionStep(
                tool=s.get("tool", "speak"),
                params=s.get("params", {}),
                description=s.get("description", "Recovery step"),
            )
        except Exception as e:
            logger.warning("Recovery parse failed: %s", e)
            return None

    async def summarize(self, plan: Optional["Plan"], results: list) -> str:
        """Generate a brief spoken summary of what was accomplished."""
        if plan is None:
            return "The command could not be processed."
        success_count = sum(1 for r in results if getattr(r, "success", True))
        if len(results) == 0:
            return f"I've planned to {plan.goal}."
        if success_count == len(results):
            return f"Done. I've completed: {plan.goal}."
        return f"Completed {success_count} of {len(results)} steps for: {plan.goal}."
