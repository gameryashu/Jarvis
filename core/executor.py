"""
core/executor.py — Action execution engine.
Dispatches ActionStep objects to the correct tool handler.
Controls mouse, keyboard, terminal, browser, files, clipboard, TTS, and more.
"""

import asyncio
import datetime
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from config.settings import Settings
from core.llm import ActionStep


# ── Module-level Action Logger ─────────────────────────────────────────────────

def _log_action(tool: str, params: dict, success: bool, detail: str = "") -> None:
    """Append a timestamped log entry to ~/.jarvis/memory/action_log.txt."""
    try:
        log_dir = Path.home() / ".jarvis" / "memory"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "action_log.txt"
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        status = "OK" if success else "FAIL"
        # Trim params for readability
        param_str = str(params)[:120]
        detail_str = (detail or "")[:200]
        line = f"[{ts}] [{status}] tool={tool} params={param_str} detail={detail_str}\n"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass  # Never let logging crash the caller


# ── Windows App Aliases ────────────────────────────────────────────────────────

_WIN_APP_ALIASES: dict[str, str] = {
    "calculator":   "calc",
    "notepad":      "notepad",
    "chrome":       "start msedge",
    "edge":         "start msedge",
    "explorer":     "explorer",
    "cmd":          "cmd",
    "powershell":   "powershell",
    "calc":         "calc",
    "mspaint":      "mspaint",
    "taskmgr":      "taskmgr",
    "spotify":      "start spotify:",
    "code":         "code",
    "antigravity":  "antigravity",
    "wt":           "wt",
}


# ── Result Dataclass ───────────────────────────────────────────────────────────

@dataclass
class ExecutionResult:
    success: bool
    output: Any = None
    error: str = ""
    step: Optional[ActionStep] = None


# ── Action Executor ────────────────────────────────────────────────────────────

class ActionExecutor:
    """
    Routes action steps to tool handlers.
    Each handler is an async method named _tool_<tool_name>.
    Maintains a persistent Playwright browser across calls.
    """

    # Class-level Playwright state — shared across all instances but
    # in practice there is only one ActionExecutor per process.
    _persistent_pw = None        # Playwright context manager handle
    _persistent_browser = None   # Browser instance
    _persistent_page = None      # Active page

    def __init__(self, settings: Settings):
        self.settings = settings
        self._tts_engine = None

    # ── Playwright Management ──────────────────────────────────────────────────

    async def _ensure_playwright(self):
        """
        Return the active Playwright page, launching browser if needed.
        Reuses the class-level persistent browser across calls.
        headless=False, slow_mo=0. Tries msedge first, falls back to chromium.
        Sets 30s default timeout on the page.
        """
        try:
            # If page already exists and is not closed, reuse it
            if ActionExecutor._persistent_page is not None:
                try:
                    # Quick sanity check — if the page is closed this raises
                    _ = ActionExecutor._persistent_page.url
                    return ActionExecutor._persistent_page
                except Exception:
                    # Page was closed externally; reset state
                    ActionExecutor._persistent_page = None
                    ActionExecutor._persistent_browser = None
                    ActionExecutor._persistent_pw = None

            from playwright.async_api import async_playwright
            pw = await async_playwright().start()
            ActionExecutor._persistent_pw = pw

            # Try Microsoft Edge first; fall back to plain chromium
            try:
                browser = await pw.chromium.launch(
                    channel="msedge",
                    headless=False,
                    slow_mo=0,
                )
            except Exception:
                browser = await pw.chromium.launch(
                    headless=False,
                    slow_mo=0,
                )

            ActionExecutor._persistent_browser = browser
            page = await browser.new_page()
            page.set_default_timeout(30_000)
            ActionExecutor._persistent_page = page
            return page

        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"Failed to start Playwright: {e}") from e

    async def _cleanup_playwright(self, force: bool = False) -> None:
        """Close the persistent browser. Only acts when force=True."""
        if not force:
            return
        try:
            if ActionExecutor._persistent_browser is not None:
                await ActionExecutor._persistent_browser.close()
            if ActionExecutor._persistent_pw is not None:
                await ActionExecutor._persistent_pw.stop()
        except (asyncio.CancelledError, Exception):
            pass
        finally:
            ActionExecutor._persistent_page = None
            ActionExecutor._persistent_browser = None
            ActionExecutor._persistent_pw = None

    # ── Dispatcher ────────────────────────────────────────────────────────────

    async def execute(self, step: ActionStep) -> ExecutionResult:
        """Dispatch a step to the appropriate tool handler with retry logic."""
        # Confirmation gate for destructive operations
        if step.is_destructive and self.settings.confirm_destructive:
            try:
                confirmed = await self._confirm(step)
            except (asyncio.CancelledError, Exception) as e:
                return ExecutionResult(
                    success=False,
                    error=f"Confirmation error: {e}",
                    step=step,
                )
            if not confirmed:
                _log_action(step.tool, step.params, False, "User declined confirmation")
                return ExecutionResult(
                    success=False,
                    error="User declined confirmation.",
                    step=step,
                )

        handler_name = f"_tool_{step.tool}"
        handler = getattr(self, handler_name, None)

        if handler is None:
            _log_action(step.tool, step.params, False, f"Unknown tool: {step.tool}")
            return ExecutionResult(
                success=False,
                error=f"Unknown tool: {step.tool}",
                step=step,
            )

        # Execute with one automatic retry on failure
        for attempt in range(2):
            try:
                result = await handler(step.params)
                _log_action(step.tool, step.params, True, str(result)[:200] if result is not None else "")
                return ExecutionResult(success=True, output=result, step=step)
            except (asyncio.CancelledError, Exception) as e:
                if attempt == 0:
                    # First failure — wait 0.5s then retry
                    await asyncio.sleep(0.5)
                else:
                    err = f"{type(e).__name__}: {e}"
                    _log_action(step.tool, step.params, False, err)
                    return ExecutionResult(success=False, error=err, step=step)

        # Should never reach here
        return ExecutionResult(success=False, error="Unexpected retry exhaustion", step=step)

    async def _confirm(self, step: ActionStep) -> bool:
        """Ask the user to confirm a destructive action."""
        print(f"\n⚠️  CONFIRM: {step.description}")
        print(f"   Tool: {step.tool}, Params: {step.params}")
        loop = asyncio.get_event_loop()
        answer = await loop.run_in_executor(None, input, "   Proceed? [y/N] ")
        return answer.strip().lower() in ("y", "yes")

    # ── Public speak helper ────────────────────────────────────────────────────

    async def speak(self, text: str) -> None:
        """Convenience method to speak text — called by main.py directly."""
        await self._tool_speak({"text": text})

    # ── Terminal ──────────────────────────────────────────────────────────────

    async def _tool_terminal(self, params: dict) -> str:
        command = params.get("command", "")
        loop = asyncio.get_event_loop()

        def _run():
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            return (result.stdout + result.stderr).strip()

        try:
            output = await loop.run_in_executor(None, _run)
            print(f"   $ {command}\n   {output[:500]}")
            return output
        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"Terminal error: {e}") from e

    # ── Mouse ─────────────────────────────────────────────────────────────────

    async def _tool_mouse_move(self, params: dict):
        import pyautogui
        x, y = params["x"], params["y"]
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None, lambda: pyautogui.moveTo(x, y, duration=self.settings.mouse_move_duration)
            )
        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"mouse_move error: {e}") from e

    async def _tool_mouse_click(self, params: dict):
        import pyautogui
        x = params.get("x")
        y = params.get("y")
        button = params.get("button", "left")
        loop = asyncio.get_event_loop()

        def _click():
            if x is not None and y is not None:
                pyautogui.moveTo(x, y, duration=self.settings.mouse_move_duration)
            if button == "double":
                pyautogui.doubleClick()
            elif button == "right":
                pyautogui.rightClick()
            else:
                pyautogui.click()

        try:
            await loop.run_in_executor(None, _click)
        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"mouse_click error: {e}") from e

    async def _tool_mouse_scroll(self, params: dict):
        import pyautogui
        x = params.get("x")
        y = params.get("y")
        direction = params.get("direction", "down")
        amount = params.get("amount", 3)
        clicks = amount if direction == "up" else -amount
        loop = asyncio.get_event_loop()

        def _scroll():
            if x is not None and y is not None:
                pyautogui.moveTo(x, y)
            pyautogui.scroll(clicks)

        try:
            await loop.run_in_executor(None, _scroll)
        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"mouse_scroll error: {e}") from e

    # ── Keyboard ──────────────────────────────────────────────────────────────

    async def _tool_type_text(self, params: dict):
        import pyautogui
        text = params.get("text", "")
        interval = params.get("interval", self.settings.typing_interval)
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None, lambda: pyautogui.write(text, interval=interval)
            )
        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"type_text error: {e}") from e

    async def _tool_key_press(self, params: dict):
        import pyautogui
        keys = params.get("keys", "")
        loop = asyncio.get_event_loop()

        def _press():
            if "+" in keys:
                parts = [k.strip() for k in keys.split("+")]
                pyautogui.hotkey(*parts)
            else:
                pyautogui.press(keys)

        try:
            await loop.run_in_executor(None, _press)
        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"key_press error: {e}") from e

    # ── Applications ──────────────────────────────────────────────────────────

    async def _tool_open_app(self, params: dict):
        """
        Launch an application on Windows.
        Resolves aliases from _WIN_APP_ALIASES and runs via shell=True.
        Supports optional 'flags' param for launch arguments.
        """
        app = params.get("app", "").strip().lower()
        flags = params.get("flags", "").strip()
        loop = asyncio.get_event_loop()

        # Resolve alias
        cmd = _WIN_APP_ALIASES.get(app, app)

        # Append flags if provided
        if flags:
            cmd = f"{cmd} {flags}"

        def _open():
            subprocess.Popen(cmd, shell=True)
            time.sleep(0.5)  # Brief pause to let the window appear

        try:
            await loop.run_in_executor(None, _open)
            print(f"   🚀 Launched: {cmd}")
        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"open_app error launching '{cmd}': {e}") from e

    # ── Screenshot & OCR ──────────────────────────────────────────────────────

    async def _tool_screenshot(self, params: dict) -> str:
        """
        Take a screenshot, save to ~/.jarvis/screenshots/<timestamp>.png,
        and return the file path string.
        """
        import pyautogui
        region = params.get("region")  # [x, y, w, h] or None
        loop = asyncio.get_event_loop()

        def _snap() -> str:
            save_dir = Path.home() / ".jarvis" / "screenshots"
            save_dir.mkdir(parents=True, exist_ok=True)
            filename = f"screenshot_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.png"
            save_path = save_dir / filename

            if region:
                img = pyautogui.screenshot(region=tuple(region))
            else:
                img = pyautogui.screenshot()

            img.save(str(save_path))
            return str(save_path)

        try:
            path = await loop.run_in_executor(None, _snap)
            print(f"   📸 Screenshot saved: {path}")
            return path
        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"screenshot error: {e}") from e

    async def _tool_ocr_read(self, params: dict) -> str:
        """Read text from the screen using pytesseract. Returns empty string if unavailable."""
        try:
            import pytesseract
            import pyautogui
        except ImportError:
            return ""

        region = params.get("region")
        loop = asyncio.get_event_loop()

        def _ocr() -> str:
            if region:
                img = pyautogui.screenshot(region=tuple(region))
            else:
                img = pyautogui.screenshot()
            lang = self.settings.ocr_language
            try:
                return pytesseract.image_to_string(img, lang=lang)
            except Exception:
                return ""

        try:
            text = await loop.run_in_executor(None, _ocr)
            return text.strip()
        except (asyncio.CancelledError, Exception):
            return ""

    # ── Browser (Playwright) ───────────────────────────────────────────────────

    async def _tool_browser_open(self, params: dict) -> str:
        """Open a URL in the persistent Playwright browser (headless=False, slow_mo=0)."""
        url = params.get("url", "")
        try:
            page = await self._ensure_playwright()
            await page.goto(url, wait_until="networkidle", timeout=30_000)
            return page.url
        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"browser_open error: {e}") from e

    async def _tool_browser_navigate(self, params: dict) -> str:
        """Navigate the current Playwright page to a URL (networkidle, 30s timeout)."""
        url = params.get("url", "")
        try:
            page = await self._ensure_playwright()
            await page.goto(url, wait_until="networkidle", timeout=30_000)
            return page.url
        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"browser_navigate error: {e}") from e

    async def _tool_browser_search(self, params: dict) -> str:
        """
        Search the web via Playwright.
        - If currently on youtube.com: tries input[name='search_query'] then
          ytd-searchbox input (15s timeout each).
        - Otherwise: navigates to a Google search URL.
        Returns the resulting page URL.
        """
        query = params.get("query", "")
        try:
            page = await self._ensure_playwright()
            current_url = page.url

            if "youtube.com" in current_url:
                # Try YouTube search box
                try:
                    search_input = page.locator("input[name='search_query']")
                    await search_input.wait_for(state="visible", timeout=15_000)
                    await search_input.fill(query)
                    await search_input.press("Enter")
                    await page.wait_for_load_state("networkidle", timeout=30_000)
                    return page.url
                except (asyncio.CancelledError, Exception):
                    pass

                try:
                    search_input = page.locator("ytd-searchbox input")
                    await search_input.wait_for(state="visible", timeout=15_000)
                    await search_input.fill(query)
                    await search_input.press("Enter")
                    await page.wait_for_load_state("networkidle", timeout=30_000)
                    return page.url
                except (asyncio.CancelledError, Exception) as e:
                    raise RuntimeError(f"YouTube search failed: {e}") from e
            else:
                # Google search URL
                encoded = query.replace(" ", "+")
                google_url = f"https://www.google.com/search?q={encoded}"
                await page.goto(google_url, wait_until="networkidle", timeout=30_000)
                return page.url

        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"browser_search error: {e}") from e

    async def _tool_browser_click(self, params: dict) -> str:
        """Click a web element by CSS/XPath selector (15s timeout)."""
        selector = params.get("selector", "")
        try:
            page = await self._ensure_playwright()
            await page.click(selector, timeout=15_000)
            return f"Clicked: {selector}"
        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"browser_click error on '{selector}': {e}") from e

    async def _tool_browser_type(self, params: dict) -> str:
        """Fill text into a web element by selector using Playwright fill (15s timeout)."""
        selector = params.get("selector", "")
        text = params.get("text", "")
        try:
            page = await self._ensure_playwright()
            await page.fill(selector, text, timeout=15_000)
            return f"Typed into: {selector}"
        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"browser_type error on '{selector}': {e}") from e

    async def _tool_browser_extract(self, params: dict) -> str:
        """Extract inner text from a web element by selector."""
        selector = params.get("selector", "")
        attribute = params.get("attribute")
        try:
            page = await self._ensure_playwright()
            el = page.locator(selector).first
            if attribute:
                result = await el.get_attribute(attribute, timeout=15_000)
                return result or ""
            else:
                result = await el.inner_text(timeout=15_000)
                return result or ""
        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"browser_extract error on '{selector}': {e}") from e

    # ── Files ─────────────────────────────────────────────────────────────────

    def _resolve_path(self, raw: str) -> Path:
        """
        Resolve a path string to a pathlib.Path.
        Handles ~ expansion and ~/Desktop → actual Windows Desktop path.
        Never uses os.path.join for Windows paths.
        """
        p = raw.strip()
        if p.startswith("~/Desktop") or p.startswith("~\\Desktop"):
            rest = p[len("~/Desktop"):].lstrip("/\\")
            base = Path.home() / "Desktop"
            return base / rest if rest else base
        return Path(p).expanduser()

    async def _tool_file_read(self, params: dict) -> str:
        path = self._resolve_path(params.get("path", ""))
        loop = asyncio.get_event_loop()

        def _read():
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()

        try:
            return await loop.run_in_executor(None, _read)
        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"file_read error: {e}") from e

    async def _tool_file_write(self, params: dict):
        path = self._resolve_path(params.get("path", ""))
        content = params.get("content", "")
        mode = params.get("mode", "w")
        loop = asyncio.get_event_loop()

        def _write():
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, mode, encoding="utf-8") as f:
                f.write(content)

        try:
            await loop.run_in_executor(None, _write)
        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"file_write error: {e}") from e

    async def _tool_file_delete(self, params: dict):
        import shutil
        path = self._resolve_path(params.get("path", ""))
        loop = asyncio.get_event_loop()

        def _delete():
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()

        try:
            await loop.run_in_executor(None, _delete)
        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"file_delete error: {e}") from e

    async def _tool_create_folder(self, params: dict) -> str:
        """
        Create a directory. If 'path' is a bare name (no separators),
        creates it on the Desktop using pathlib.Path.home() / 'Desktop' / name.
        Never uses string concatenation for paths.
        """
        raw = params.get("path", params.get("name", ""))
        loop = asyncio.get_event_loop()

        def _make() -> str:
            p = raw.strip()
            # If it's just a name (no slashes), put it on the Desktop
            if p and "/" not in p and "\\" not in p and ":" not in p:
                folder = Path.home() / "Desktop" / p
            else:
                folder = self._resolve_path(p)
            folder.mkdir(parents=True, exist_ok=True)
            return str(folder)

        try:
            result = await loop.run_in_executor(None, _make)
            print(f"   📁 Created folder: {result}")
            return result
        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"create_folder error: {e}") from e

    # ── Clipboard ─────────────────────────────────────────────────────────────

    async def _tool_clipboard_copy(self, params: dict):
        try:
            import pyperclip
            text = params.get("text", "")
            loop = asyncio.get_event_loop()
            try:
                await loop.run_in_executor(None, lambda: pyperclip.copy(text))
            except (asyncio.CancelledError, Exception) as e:
                raise RuntimeError(f"clipboard_copy error: {e}") from e
        except ImportError:
            print("⚠️  pyperclip not installed: pip install pyperclip")

    async def _tool_clipboard_paste(self, params: dict) -> str:
        try:
            import pyperclip
            loop = asyncio.get_event_loop()
            try:
                return await loop.run_in_executor(None, pyperclip.paste)
            except (asyncio.CancelledError, Exception) as e:
                raise RuntimeError(f"clipboard_paste error: {e}") from e
        except ImportError:
            return ""

    # ── TTS / Notifications ───────────────────────────────────────────────────

    async def _tool_speak(self, params: dict):
        """
        Speak text using edge-tts (en-US-GuyNeural).
        Falls back to print() if cancelled or unavailable.
        """
        text = params.get("text", "")
        if not text:
            return

        loop = asyncio.get_event_loop()

        async def _edge_tts_speak():
            try:
                import edge_tts
                import tempfile
                import playsound

                communicate = edge_tts.Communicate(text, "en-US-GuyNeural")
                tmp_path = None
                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                    tmp_path = Path(f.name)

                try:
                    await communicate.save(str(tmp_path))
                    await loop.run_in_executor(None, playsound.playsound, str(tmp_path))
                except asyncio.CancelledError:
                    print(f"🔈 {text}")
                finally:
                    try:
                        if tmp_path and tmp_path.exists():
                            tmp_path.unlink()
                    except Exception:
                        pass
            except ImportError:
                print(f"🔈 {text}  (install edge-tts and playsound)")
            except asyncio.CancelledError:
                print(f"🔈 {text}")
            except Exception:
                print(f"🔈 {text}")

        provider = self.settings.tts_provider
        if provider in ("edge-tts", "pyttsx3"):
            try:
                await _edge_tts_speak()
            except (asyncio.CancelledError, Exception):
                print(f"🔈 {text}")
        elif provider == "elevenlabs":
            try:
                await self._elevenlabs_speak(text)
            except (asyncio.CancelledError, Exception):
                print(f"🔈 {text}")
        elif provider == "gtts":
            def _gtts():
                try:
                    from gtts import gTTS
                    import tempfile
                    import playsound
                    tts = gTTS(text=text, lang="en")
                    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                        tmp = Path(f.name)
                    tts.save(str(tmp))
                    playsound.playsound(str(tmp))
                    tmp.unlink(missing_ok=True)
                except Exception:
                    print(f"🔈 {text}")
            try:
                await loop.run_in_executor(None, _gtts)
            except (asyncio.CancelledError, Exception):
                print(f"🔈 {text}")
        else:
            print(f"🔈 {text}")

    async def _elevenlabs_speak(self, text: str):
        try:
            import httpx
            import tempfile
            import playsound

            api_key = self.settings.tts_elevenlabs_key
            voice_id = self.settings.tts_elevenlabs_voice_id or "21m00Tcm4TlvDq8ikWAM"
            loop = asyncio.get_event_loop()

            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                    headers={"xi-api-key": api_key},
                    json={"text": text, "model_id": "eleven_monolingual_v1"},
                )
                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                    tmp = Path(f.name)
                    f.write(resp.content)
                await loop.run_in_executor(None, lambda: playsound.playsound(str(tmp)))
                tmp.unlink(missing_ok=True)
        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"ElevenLabs TTS error: {e}") from e

    async def _tool_notify(self, params: dict):
        title = params.get("title", "JARVIS")
        message = params.get("message", "")
        try:
            from plyer import notification
            loop = asyncio.get_event_loop()
            try:
                await loop.run_in_executor(
                    None,
                    lambda: notification.notify(title=title, message=message, timeout=5),
                )
            except (asyncio.CancelledError, Exception) as e:
                print(f"🔔 {title}: {message}")
        except ImportError:
            print(f"🔔 {title}: {message}")

    async def _tool_wait(self, params: dict):
        seconds = float(params.get("seconds", 1.0))
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            pass
