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

SYSTEM_PROMPT = r"""To build this accurately within your 800-token limit, what specific mechanics from each tool are you trying to replicate in the execution loop? Define the exact behaviors you need—such as Perplexity's search-to-action grounding, OpenClaw's screen parsing, or Claude's task decomposition—and I will extract the exact logic from your uploaded prompt files to synthesize the final system instruction.

1. Task Decomposition (Claude-style)
The behavior I need:
* Given `"open spotify and play lofi"`, decompose into ordered atomic steps: `open_app → wait → play_spotify`
* Recognize implicit dependencies between steps (don't click a button before the window exists)
* Know when a single step is sufficient vs. when a task truly needs 3+ steps
Current gap: The LLM sometimes emits 5 steps for a 1-step task, or 1 step for a task that requires UI verification first.
2. Screen-State Grounding (OpenClaw/computer-use style)
The behavior I need:
* Before any
mouse_click, emit a
screenshot +
ocr_read step to verify the target UI element actually exists
* After a destructive or long-running action, emit a
screenshot to confirm the screen changed
* If the OCR text doesn't match what's expected → flag the step as needing recovery, not just pass
Current gap: The
feedback.py_verify_ui_action()sleeps 0.5s and runs OCR, but theplannerdoesn't request screenshot steps proactively — it only does so reactively. The grounding needs to happen at plan-time.

3. Search-to-Action Grounding (Perplexity-style)
The behavior I need:
* When the command is ambiguous (`"play something chill"`), resolve it to a concrete query before executing — don't delegate ambiguity to YouTube's search algorithm
* For file/folder paths, infer the absolute path from context (`"desktop"` → `C:\Users\yashu\Desktop`) rather than passing relative paths that fail
Current gap: The system prompt says `Path: C:\Users\yashu\Desktop` but the LLM still sometimes emits `~/Desktop` or relative paths in `file_write` params.
4. Failure-Aware Replanning (Agent loop style)
The behavior I need:
* If step N fails, the loop should tell the LLM what failed and why, then ask for a single corrective step — not re-plan the entire task from scratch
* Track `failed_tools` set so the same broken approach isn't retried
* Max 5 total attempts across the whole session, not per-step
Current gap: `main.py`'s `_autonomous_loop()` already has `failed_tools` tracking, but `planner.recover()` gets sent the full failure context and sometimes re-generates the entire original plan instead of one targeted fix.
5. JSON Output Enforcement
The behavior I need:
* The LLM never wraps output in markdown fences (```json...```)
* Every response has `"goal"` and `"steps"` at the top level — no nesting surprises
* `params` always has the exact keys the tool handler expects (no extra/missing keys)
Current gap: Groq models occasionally emit \```json`fences even with`response_format={"type": "json_object"}`set. The`_parse_plan()` method strips these, but it should never need to.
What I Need From You
If you have uploaded prompt files from Perplexity, OpenClaw, or other agent systems, the exact extractions I need are:
MechanicExtract ThisScreen groundingThe exact instruction that forces a screenshot step before any clickPath resolutionThe rule that maps `"desktop"` / `"downloads"` → absolute Windows pathsRecovery framingThe exact prompt structure for single-step corrective replanningAmbiguity resolutionThe rule that forces the LLM to pick a concrete interpretation and state it in `"goal"`JSON strictnessThe instruction pattern that eliminates markdown wrapping in JSON-mode models
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
