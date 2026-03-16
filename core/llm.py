"""
core/llm.py — LLM reasoning and task planning.
Converts natural language commands into structured, executable action plans.
Supports Anthropic Claude, OpenAI GPT, and local Ollama models.
"""

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional
from config.settings import Settings
from core.memory import MemoryManager


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

SYSTEM_PROMPT = """You are JARVIS, an expert AI system controller.
Your job is to translate a user's natural language command into a precise, executable action plan.

IDENTITY:
- JARVIS is running on Windows 11, user is Yatharth, laptop is HP Omen with RTX 4050
- Default browser is Microsoft Edge (Chrome is NOT installed)
- Desktop path is C:\\Users\\yashu\\Desktop
- User is a 15-year-old programmer and gamer

You have access to these tools:
- terminal: Run shell commands. params: {command: str}
- mouse_move: Move mouse to coordinates. params: {x: int, y: int}
- mouse_click: Click mouse. params: {x: int, y: int, button: "left"|"right"|"double"}
- mouse_scroll: Scroll. params: {x: int, y: int, direction: "up"|"down", amount: int}
- type_text: Type text. params: {text: str}
- key_press: Press keyboard shortcuts. params: {keys: str} e.g. "ctrl+c"
- open_app: Launch an application. params: {app: str, flags: str (optional)}
  Supports optional flags for launch arguments.
  Examples:
    {"tool": "open_app", "params": {"app": "edge", "flags": "--inprivate"}}
- screenshot: Take a screenshot. params: {region: null | [x,y,w,h]}
- ocr_read: Read text from screen region. params: {region: null | [x,y,w,h]}
- browser_open: Open URL in browser (Playwright). params: {url: str}
- browser_search: Search the web (Playwright). params: {query: str, engine: "google"|"duckduckgo"}
- browser_click: Click a web page element (Playwright). params: {selector: str} (CSS or XPath selector)
- browser_type: Type text into a web page input (Playwright). params: {selector: str, text: str}
- browser_navigate: Navigate Playwright page to a URL. params: {url: str}
- browser_extract: Extract text from a web element (Playwright). params: {selector: str, attribute: str (optional)}
- file_read: Read a file. params: {path: str}
- file_write: Write to a file. params: {path: str, content: str, mode: "w"|"a"}
- file_delete: Delete a file/directory. params: {path: str}
- create_folder: Create a directory. params: {path: str}
- clipboard_copy: Copy text to clipboard. params: {text: str}
- clipboard_paste: Get current clipboard content. params: {}
- speak: Say something aloud. params: {text: str}
- wait: Pause execution. params: {seconds: float}
- notify: Show desktop notification. params: {title: str, message: str}

INTENT INFERENCE RULES:
- "that browser thing" / "the browser" / "browser" -> open Edge
- "search for X" without specifying app -> browser_search in Edge
- "open X" where X is vague -> try to match to closest known app
- "youtube" alone -> browser_open https://youtube.com
- "github" alone -> browser_open https://github.com
- "gmail" alone -> browser_open https://gmail.com
- Single word that matches an app name -> open that app
- "play X" -> search YouTube for X
- "find X" / "look up X" / "google X" -> browser_search for X
- "code" / "coding" / "editor" -> open vscode or antigravity
- "files" / "folder" -> open explorer
- "terminal" / "console" -> open cmd or powershell
- If command is completely unclear -> use speak tool to ask one clarifying question

CONTEXT RULES:
- Always prefer Edge over Chrome
- Always use C:\\Users\\yashu\\Desktop for desktop paths, never ~/Desktop
- For multi-step tasks, break into smallest possible atomic steps
- Always add wait(1.0) after open_app before any keyboard/mouse interaction
- For browser searches, use browser_search tool, not type_text

RESPONSE RULES:
- Always output valid JSON, no markdown
- If genuinely ambiguous between 2 interpretations, pick the most likely one and proceed
- Never ask for clarification unless the command is completely meaningless
- Assume the user wants the fastest path to their goal
- Mark is_destructive: true for file_delete, file_write (overwrite), or terminal commands that modify system state.

RESPONSE FORMAT (strict JSON only):
{"goal": "one-sentence description", "steps": [{"tool": "tool_name", "params": {}, "description": "human-readable", "requires_confirmation": false, "is_destructive": false}]}
"""


# ── JSON Extraction Helpers ───────────────────────────────────────────────────

def _extract_json(raw: str) -> dict | None:
    """Try multiple strategies to extract valid JSON from an LLM response."""
    text = raw.strip()

    # 1. Strip markdown code fences
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```", "", text)
    text = text.strip()

    # 2. Direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 3. Find the outermost { ... } block
    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start != -1 and brace_end > brace_start:
        candidate = text[brace_start : brace_end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    # 4. Regex: greedy match of a JSON-like object
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass

    # 5. Try fixing common issues: trailing commas
    cleaned = re.sub(r",\s*([}\]])", r"\1", text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    return None


# ── LLM Planner ───────────────────────────────────────────────────────────────

class LLMPlanner:
    def __init__(self, settings: Settings, memory: MemoryManager):
        self.settings = settings
        self.memory = memory
        self._client = self._init_client()

    def _init_client(self):
        provider = self.settings.llm_provider
        if provider == "anthropic":
            try:
                import anthropic
                return anthropic.Anthropic(api_key=self.settings.llm_api_key)
            except ImportError:
                raise ImportError("Install anthropic: pip install anthropic")
        elif provider == "openai":
            try:
                import openai
                api_key = os.environ.get("OPENAI_API_KEY") or self.settings.llm_api_key
                base_url = os.environ.get("OPENAI_BASE_URL") or self.settings.llm_base_url
                print(f"  🔑 OpenAI api_key starts with: {api_key[:10]}..." if api_key else "  ⚠️ No API key found!")
                kwargs = {"api_key": api_key}
                if base_url:
                    kwargs["base_url"] = base_url
                client = openai.OpenAI(**kwargs)
                return client
            except ImportError:
                raise ImportError("Install openai: pip install openai")
        elif provider == "ollama":
            try:
                import ollama
                return ollama
            except ImportError:
                raise ImportError("Install ollama: pip install ollama")
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
        result = self._parse_plan(command, raw)
        self._last_plan = result
        return result

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
        """Parse LLM JSON response into a Plan object.
        Uses multi-strategy extraction to handle messy LLM output."""
        data = _extract_json(raw)

        if data is None:
            # All extraction strategies failed — provide a clear fallback
            print(f"  ⚠️  LLM returned non-JSON. Raw ({len(raw)} chars): {raw[:120]}...")
            return Plan(
                goal=original_command,
                steps=[ActionStep(
                    tool="speak",
                    params={"text": f"I couldn't parse a plan for that. Please try rephrasing."},
                    description="Report parse failure",
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

        if not steps:
            # JSON parsed but had no steps — still provide feedback
            return Plan(
                goal=data.get("goal", original_command),
                steps=[ActionStep(
                    tool="speak",
                    params={"text": f"I understood your request but generated no action steps."},
                    description="Empty plan notification",
                )],
                raw_response=raw,
            )

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
            f"Suggest ONE recovery action in the same JSON step format.\n"
            f"Reply with a JSON object only, no extra text."
        )
        loop = asyncio.get_event_loop()
        raw = await loop.run_in_executor(None, self._call_llm, prompt)

        data = _extract_json(raw)
        if data is None:
            return None

        try:
            if "steps" in data and data["steps"]:
                s = data["steps"][0]
            else:
                s = data
            return ActionStep(
                tool=s.get("tool", "speak"),
                params=s.get("params", {}),
                description=s.get("description", "Recovery step"),
            )
        except Exception:
            return None

    async def summarize(self, plan: Plan, results: list) -> str:
        """Generate a brief spoken summary of what was accomplished."""
        success_count = sum(1 for r in results if getattr(r, "success", True))
        if len(results) == 0:
            return f"I've planned to {plan.goal}."
        if success_count == len(results):
            return f"Done. I've completed: {plan.goal}."
        return f"Completed {success_count} of {len(results)} steps for: {plan.goal}."

    async def is_goal_complete(
        self, goal: str, history: list[str], vision_feedback: str
    ) -> bool:
        """Ask the LLM whether the user's original goal is satisfied.
        Used by the autonomous completion loop to decide when to stop."""
        import asyncio
        history_str = "\n".join(f"  {i+1}. {h}" for i, h in enumerate(history))
        prompt = (
            f"Original goal: {goal}\n\n"
            f"Steps completed so far:\n{history_str}\n\n"
            f"Latest screen analysis: {vision_feedback}\n\n"
            f"Based on the steps completed and the current screen state, "
            f"is the original goal fully achieved?\n"
            f'Respond with ONLY a JSON object: {{"complete": true/false, "reason": "brief explanation"}}'
        )
        loop = asyncio.get_event_loop()
        raw = await loop.run_in_executor(None, self._call_llm, prompt)

        data = _extract_json(raw)
        if data is None:
            return False
        return bool(data.get("complete", False))
