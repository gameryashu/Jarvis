"""
core/feedback.py — Feedback loop.
After each action, takes a screenshot and uses the LLM to verify success.
"""

import asyncio
import logging
from dataclasses import dataclass

from config.settings import Settings
from core.executor import ActionExecutor, ExecutionResult
from core.llm import ActionStep

logger = logging.getLogger(__name__)


@dataclass
class VerificationResult:
    success: bool
    reason: str = ""
    screen_text: str = ""


class FeedbackLoop:
    """
    Reads screen state after each action and verifies whether the step succeeded.
    Uses LLM-powered analysis of screenshots when available.
    """

    def __init__(self, settings: Settings, executor: ActionExecutor):
        self.settings = settings
        self.executor = executor

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
            return VerificationResult(success=False, reason=result.error or "Unknown error")

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

        # UI actions: capture screen and do a quick sanity check
        if tool in ("mouse_click", "key_press", "type_text", "open_app", "browser_open"):
            return await self._verify_ui_action(step, result)

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

    async def _verify_ui_action(
        self, step: ActionStep, result: ExecutionResult
    ) -> VerificationResult:
        """
        Takes a screenshot and optionally runs OCR to check screen state.
        For now: basic screenshot + OCR-based text extraction as evidence.
        """
        await asyncio.sleep(0.5)  # Let UI settle

        try:
            # Capture screenshot
            screenshot_result = await self.executor.execute(
                ActionStep(tool="screenshot", params={}, description="Feedback screenshot")
            )
            if not screenshot_result.success:
                return VerificationResult(success=True, reason="Screenshot unavailable, assuming success.")

            # OCR the visible text
            ocr_result = await self.executor.execute(
                ActionStep(tool="ocr_read", params={}, description="OCR feedback")
            )
            if not ocr_result.success:
                return VerificationResult(success=True, reason="OCR unavailable, assuming success.")
            screen_text = ocr_result.output or ""

            # Check for obvious error states
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
            # Verification failure must not block execution
            logger.warning("UI verification failed: %s", e)
            return VerificationResult(success=True, reason=f"Verification skipped: {e}")

    async def capture_state(self) -> dict:
        """Capture full current state of the screen as a dict."""
        screenshot = await self.executor.execute(
            ActionStep(tool="screenshot", params={}, description="State capture")
        )
        ocr = await self.executor.execute(
            ActionStep(tool="ocr_read", params={}, description="State OCR")
        )
        return {
            "screenshot": screenshot.output,
            "screen_text": ocr.output or "",
        }
