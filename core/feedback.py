"""
core/feedback.py — Feedback loop.
After each action, takes a screenshot and uses Gemini Vision to verify success.
Falls back to OCR-based keyword scan when the vision API is unavailable.
"""

import asyncio
import base64
import io
import json
import os
from dataclasses import dataclass
from typing import Optional

from config.settings import Settings
from core.executor import ActionExecutor, ExecutionResult
from core.llm import ActionStep


@dataclass
class VerificationResult:
    success: bool
    reason: str = ""
    screen_text: str = ""


class FeedbackLoop:
    """
    Reads screen state after each action and verifies whether the step succeeded.
    Uses Gemini Vision for intelligent screenshot analysis when available,
    falls back to OCR keyword scanning otherwise.
    """

    # Max seconds to spend on UI verification before assuming success.
    VERIFY_TIMEOUT: float = 10.0

    def __init__(self, settings: Settings, executor: ActionExecutor):
        self.settings = settings
        self.executor = executor
        self._init_vision()

    def _init_vision(self):
        """Check if Gemini Vision is available."""
        if os.environ.get("GEMINI_API_KEY"):
            try:
                import openai
                print("👁️  Gemini Vision verification enabled.")
            except ImportError:
                print("⚠️  openai package not installed. Vision disabled. "
                      "Run: pip install openai")
        else:
            print("⚠️  No GEMINI_API_KEY set. Falling back to OCR verification.")

    async def verify(self, step: ActionStep, result: ExecutionResult) -> VerificationResult:
        """
        Verify that a step produced the expected outcome.
        Logic:
        1. If the step itself returned an error, fail immediately.
        2. For UI-affecting steps (mouse, keyboard, open_app), capture screen and analyze.
        3. For terminal steps, check command output for error patterns.
        4. For file operations, check file existence/content.
        """
        if not result.success:
            return VerificationResult(
                success=False,
                reason=f"Step '{step.tool}' failed: {result.error}",
            )

        tool = step.tool

        # Terminal: check output for common error patterns
        if tool == "terminal":
            return self._verify_terminal(result)

        # File write: check file exists
        if tool == "file_write":
            return await self._verify_file_write(step)

        # File delete: check file gone
        if tool == "file_delete":
            return await self._verify_file_delete(step)

        # Folder creation: check it exists
        if tool == "create_folder":
            return await self._verify_create_folder(step)

        # UI actions: capture screen with a timeout so verification never blocks
        if tool in ("mouse_click", "key_press", "type_text", "open_app",
                     "browser_open", "browser_click", "browser_type",
                     "browser_navigate"):
            return await self._verify_ui_action_safe(step, result)

        # Default: trust the result
        return VerificationResult(success=True)

    def _verify_terminal(self, result: ExecutionResult) -> VerificationResult:
        output = str(result.output or "")
        error_indicators = [
            "command not found", "permission denied", "no such file",
            "error:", "traceback", "fatal:", "exception",
        ]
        for indicator in error_indicators:
            if indicator in output.lower():
                return VerificationResult(
                    success=False,
                    reason=f"Terminal error detected: {output[:300]}",
                )
        return VerificationResult(success=True, reason=output[:200])

    async def _verify_file_write(self, step: ActionStep) -> VerificationResult:
        import os
        path = step.params.get("path", "")
        expanded = os.path.expanduser(path)
        if os.path.exists(expanded):
            return VerificationResult(success=True)
        return VerificationResult(success=False, reason=f"File not created: {path}")

    async def _verify_file_delete(self, step: ActionStep) -> VerificationResult:
        import os
        path = step.params.get("path", "")
        expanded = os.path.expanduser(path)
        if not os.path.exists(expanded):
            return VerificationResult(success=True)
        return VerificationResult(success=False, reason=f"File still exists: {path}")

    async def _verify_create_folder(self, step: ActionStep) -> VerificationResult:
        import os
        path = step.params.get("path", "")
        expanded = os.path.expanduser(path)
        if os.path.isdir(expanded):
            return VerificationResult(success=True)
        return VerificationResult(success=False, reason=f"Folder not created: {path}")

    async def _verify_ui_action_safe(
        self, step: ActionStep, result: ExecutionResult
    ) -> VerificationResult:
        """Run UI verification with a timeout. If it takes too long, assume success."""
        try:
            return await asyncio.wait_for(
                self._verify_ui_action(step, result),
                timeout=self.VERIFY_TIMEOUT,
            )
        except asyncio.TimeoutError:
            return VerificationResult(
                success=True,
                reason="UI verification timed out — assuming success.",
            )

    async def _verify_ui_action(
        self, step: ActionStep, result: ExecutionResult
    ) -> VerificationResult:
        """
        Takes a screenshot and uses Vision (or OCR fallback) to verify.
        """
        await asyncio.sleep(0.5)  # Let UI settle

        try:
            # Capture screenshot (returns file path)
            screenshot_result = await self.executor.execute(
                ActionStep(tool="screenshot", params={}, description="Feedback screenshot"),
                _retry=False,
            )
            if not screenshot_result.success:
                return VerificationResult(
                    success=True, reason="Screenshot unavailable, assuming success."
                )

            image_path = str(screenshot_result.output)

            # ── Try Vision and let it fallback internally ────────────────
            return await self._analyze_with_vision(image_path, step.description)

        except Exception as e:
            # If verification itself fails, don't block execution
            return VerificationResult(success=True, reason=f"Verification skipped: {e}")

    async def _analyze_with_vision(
        self, image_path: str, step_description: str
    ) -> VerificationResult:
        """
        Send screenshot to Gemini Vision and ask whether the step succeeded.
        Returns a VerificationResult parsed from the model's JSON response.
        """
        gemini_api_key = os.environ.get('GEMINI_API_KEY')
        if not gemini_api_key:
            return await self._verify_with_ocr()

        loop = asyncio.get_event_loop()

        def _call_vision():
            from PIL import Image
            import openai

            client = openai.OpenAI(
                api_key=gemini_api_key,
                base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
            )

            # Resize image to fit within vision_max_image_size
            max_dim = self.settings.vision_max_image_size
            img = Image.open(image_path)
            img.thumbnail((max_dim, max_dim), Image.LANCZOS)

            # Convert to base64 PNG
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

            step_desc = step_description
            messages = [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    {"type": "text", "text": f"Did this action succeed: {step_desc}? Reply JSON only: {{success: bool, reason: str}}"}
                ]
            }]

            response = client.chat.completions.create(
                model="gemini-2.0-flash",
                messages=messages,
                max_tokens=256,
            )

            return response.choices[0].message.content

        try:
            raw = await loop.run_in_executor(None, _call_vision)

            # Parse JSON response from Gemini
            try:
                data = json.loads(raw.strip())
            except json.JSONDecodeError:
                # Try extracting JSON from response
                import re
                m = re.search(r'\{[\s\S]*\}', raw)
                if m:
                    data = json.loads(m.group())
                else:
                    return VerificationResult(
                        success=True,
                        reason=f"Vision returned non-JSON, assuming success: {raw[:100]}",
                    )

            return VerificationResult(
                success=bool(data.get("success", True)),
                reason=data.get("reason", "Vision analysis complete."),
            )

        except Exception as e:
            # Vision API failed — don't block execution
            print(f"  ⚠️  Vision API error: {e}. Falling back to OCR.")
            return await self._verify_with_ocr()

    async def _verify_with_ocr(self) -> VerificationResult:
        """Fallback: use OCR to read screen text and check for error keywords."""
        try:
            ocr_result = await self.executor.execute(
                ActionStep(tool="ocr_read", params={}, description="OCR feedback"),
                _retry=False,
            )
            screen_text = str(ocr_result.output or "")

            error_keywords = ["error", "not found", "access denied", "crash", "exception"]
            for kw in error_keywords:
                if kw in screen_text.lower():
                    return VerificationResult(
                        success=False,
                        reason=f"Screen shows possible error ('{kw}' detected)",
                        screen_text=screen_text[:500],
                    )

            return VerificationResult(
                success=True,
                reason="UI action completed, no errors detected.",
                screen_text=screen_text[:500],
            )
        except Exception as e:
            return VerificationResult(success=True, reason=f"OCR fallback skipped: {e}")

    async def capture_state(self) -> dict:
        """Capture full current state of the screen as a dict."""
        screenshot = await self.executor.execute(
            ActionStep(tool="screenshot", params={}, description="State capture"),
            _retry=False,
        )

        state = {
            "screenshot": screenshot.output,
            "analysis": "",
        }

        image_path = str(screenshot.output) if screenshot.success else None

        # Use vision if available, otherwise OCR
        if os.environ.get("GEMINI_API_KEY") and image_path:
            result = await self._analyze_with_vision(
                image_path, "Capture current screen state"
            )
            state["analysis"] = result.reason
        else:
            ocr = await self.executor.execute(
                ActionStep(tool="ocr_read", params={}, description="State OCR"),
                _retry=False,
            )
            state["analysis"] = str(ocr.output or "")

        return state
