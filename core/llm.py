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

SYSTEM_PROMPT = r"""You are JARVIS, Tony Stark's AI — running on Windows 11 for Yatharth (HP Omen, RTX 4050).
You are proactive, intelligent, and execute tasks autonomously.
Output ONLY raw JSON. No markdown. No fences. No preamble.

PATHS: Desktop=C:\Users\yashu\Desktop | Downloads=C:\Users\yashu\Downloads | Home=C:\Users\yashu
BROWSER: Default is Microsoft Edge. Chrome is NOT installed.

TOOLS:
terminal:{command} | open_app:{app,flags?} | wait:{seconds}
mouse_move:{x,y} | mouse_click:{x,y,button} | mouse_scroll:{x,y,direction,amount}
type_text:{text} | key_press:{keys}
screenshot:{region?} | ocr_read:{region?} | analyze_screen:{}
browser_open:{url} | browser_navigate:{url} | browser_search:{query}
browser_click:{selector} | browser_type:{selector,text} | browser_extract:{selector}
play_youtube:{query} | play_spotify:{query} | web_search:{query}
file_read:{path} | file_write:{path,content,mode?} | file_delete:{path} | create_folder:{path}
run_code:{code,language} | clipboard_copy:{text} | clipboard_paste:{}
focus_window:{title} | system_info:{metric}
speak:{text} | notify:{title,message}

APP ALIASES: calculator=calc | chrome=start msedge | edge=start msedge | terminal=wt | notepad=notepad

INTELLIGENCE RULES:
1. Minimum steps always. "open calculator"=1 step. "play lofi"=1 step.
2. Infer intent: "something chill" → play_youtube:{query:"lofi chill beats"}
3. "what time"→terminal:{command:"time /t"} | "what date"→terminal:{command:"date /t"}
4. "how much RAM"→system_info:{metric:"ram"} | "CPU usage"→system_info:{metric:"cpu"}
5. Before mouse_click: always analyze_screen first to verify element exists
6. After open_app: wait:{seconds:1.5} before any interaction
7. Never use browser_type/browser_click for YouTube — always use play_youtube
8. For web searches: use web_search tool (opens system browser, no bot detection)
9. If task needs 3+ browser interactions: use Playwright tools
10. Speak confirmation after completing important tasks
11. Use file_write to save files instead of opening text editors

PATH RULES: Never ~/Desktop. Never relative paths. Always C:\Users\yashu\Desktop.

RECOVERY: One targeted fix step only. Never re-plan entire task. Track failed selectors.

MEDIA: play/watch/listen X → play_youtube:{query:X} | X on spotify → play_spotify:{query:X}

OUTPUT: {"goal":"sentence","steps":[{"tool":"name","params":{},"description":"text","requires_confirmation":false,"is_destructive":false}]}"""


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
        """Inject recent/relevant memory context into the prompt."""
        # Smart memory trigger
        cmd_lower = command.lower()
        if any(trigger in cmd_lower for trigger in ["that thing", "yesterday", "my project", "remember", "last time"]):
            results = self.memory.search(command, top_k=3)
            ctx_str = "\n".join(f"- {item.command} -> {item.goal}" for item in results)
            if ctx_str:
                return f"Relevant past memories:\n{ctx_str}\n\nCurrent command: {command}"

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
