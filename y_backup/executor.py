"""
core/executor.py — Action execution engine.
Dispatches ActionStep objects to the correct tool handler.
Controls mouse, keyboard, terminal, browser, files, clipboard, TTS, and more.
"""

import asyncio
import json
import os
import re
import subprocess
import time  # noqa: F401
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from config.settings import Settings
from core.llm import ActionStep


# ── Windows app aliases ───────────────────────────────────────────────────────
# Maps common names to the actual executable or shell command on Windows.
_WIN_APP_ALIASES: dict[str, str] = {
    "calculator":   "calc",
    "calc":         "calc",
    "notepad":      "notepad",
    "paint":        "mspaint",
    "chrome":       "start chrome",
    "google chrome":"start chrome",
    "firefox":      "start firefox",
    "edge":         "start msedge",
    "explorer":     "explorer",
    "file explorer":"explorer",
    "cmd":          "cmd",
    "powershell":   "powershell",
    "terminal":     "wt",
    "settings":     "start ms-settings:",
    "store":        "start ms-windows-store:",
    "spotify":      "start spotify:",
    "word":         "start winword",
    "excel":        "start excel",
    "outlook":      "start outlook",
    "teams":        "start msteams:",
    "vscode":       "code",
    "vs code":      "code",
    "snipping tool":"snippingtool",
    "task manager": "taskmgr",
}

# Chrome fallback paths for when 'start chrome' is not on PATH
_CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def _try_chrome_fallbacks(flags: str = ""):
    """Try to launch Chrome using known install paths."""
    for chrome_path in _CHROME_PATHS:
        try:
            cmd = [chrome_path] + (flags.split() if flags else [])
            subprocess.Popen(cmd)
            return
        except (OSError, FileNotFoundError):
            continue
    raise FileNotFoundError(
        f"Chrome not found. Tried 'start chrome' and paths: {_CHROME_PATHS}"
    )


@dataclass
class ExecutionResult:
    success: bool
    output: Any = None
    error: str = ""
    step: Optional[ActionStep] = None


# ── Action Logger ─────────────────────────────────────────────────────────────

def _log_action(tool: str, params: dict, success: bool, detail: str = ""):
    """Append a line to ~/.jarvis/memory/action_log.txt."""
    try:
        log_dir = Path.home() / ".jarvis" / "memory"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "action_log.txt"
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        status = "OK" if success else "FAIL"
        params_short = json.dumps(params, default=str)[:200]
        line = f"[{ts}] [{status}] {tool} | {params_short}"
        if detail:
            line += f" | {detail}"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass  # logging must never crash the system


def _resolve_desktop() -> str:
    """Return the real Windows Desktop path for the current user."""
    if os.name == "nt":
        return str(Path.home() / "Desktop")
    return str(Path("~/Desktop").expanduser())


def _resolve_path(raw: str) -> Path:
    """Expand ~, ~/Desktop, macOS-style /Users/*/Desktop, and common Windows quirks."""
    if not raw:
        return Path(".")

    desktop = _resolve_desktop()

    # Replace macOS-style /Users/<username>/Desktop with real Windows Desktop
    raw = re.sub(r'^/Users/[^/]+/Desktop', desktop, raw)

    # Replace unix-style ~/Desktop with actual Windows path
    if raw.startswith("~/Desktop") or raw.startswith("~\\Desktop"):
        raw = raw.replace("~/Desktop", desktop, 1)
        raw = raw.replace("~\\Desktop", desktop, 1)

    return Path(raw).expanduser()


class ActionExecutor:
    """
    Routes action steps to tool handlers.
    Each handler is an async method named _tool_<tool_name>.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._tts_engine = None
        self._init_tts()

    def _init_tts(self):
        # We will use edge-tts which is completely async and does not need persistent engine init
        self._tts_engine = None

    # ── Dispatcher ────────────────────────────────────────────────────────────

    async def execute(self, step: ActionStep, *, _retry: bool = True) -> ExecutionResult:
        """Dispatch a step to the appropriate tool handler.
        If _retry is True (default), failed steps are retried once."""
        # Confirmation gate for destructive operations
        if step.is_destructive and self.settings.confirm_destructive:
            confirmed = await self._confirm(step)
            if not confirmed:
                result = ExecutionResult(
                    success=False,
                    error="User declined confirmation.",
                    step=step,
                )
                _log_action(step.tool, step.params, False, "declined")
                return result

        handler_name = f"_tool_{step.tool}"
        handler = getattr(self, handler_name, None)

        if handler is None:
            err = f"Unknown tool '{step.tool}'. Available tools: {self._list_tools()}"
            _log_action(step.tool, step.params, False, err)
            return ExecutionResult(success=False, error=err, step=step)

        try:
            result_value = await handler(step.params)
            _log_action(step.tool, step.params, True)
            return ExecutionResult(success=True, output=result_value, step=step)
        except Exception as e:
            err_msg = f"[{step.tool}] {type(e).__name__}: {e}"
            # ── Retry once on failure ─────────────────────────────────────
            if _retry:
                print(f"  🔄 Retrying failed step: {step.description} ({e})")
                await asyncio.sleep(0.5)
                retry_result = await self.execute(step, _retry=False)
                if retry_result.success:
                    _log_action(step.tool, step.params, True, "succeeded on retry")
                    return retry_result
                # both attempts failed — fall through
                err_msg = f"[{step.tool}] Failed after retry: {e}"

            _log_action(step.tool, step.params, False, err_msg)
            return ExecutionResult(success=False, error=err_msg, step=step)

    def _list_tools(self) -> str:
        """Return comma-separated list of available tool names."""
        return ", ".join(
            name[6:] for name in dir(self)
            if name.startswith("_tool_") and callable(getattr(self, name))
        )

    async def _confirm(self, step: ActionStep) -> bool:
        """Ask user to confirm destructive action.
        Skips confirmation for file_write when the target file doesn't exist yet."""
        # Don't block creation of brand-new files
        if step.tool == "file_write":
            target = _resolve_path(step.params.get("path", ""))
            if not target.exists():
                return True

        print(f"\n⚠️  CONFIRM: {step.description}")
        print(f"   Tool: {step.tool}, Params: {step.params}")
        loop = asyncio.get_event_loop()
        answer = await loop.run_in_executor(None, input, "   Proceed? [y/N] ")
        return answer.strip().lower() in ("y", "yes")

    # ── Terminal ──────────────────────────────────────────────────────────────

    async def _tool_terminal(self, params: dict) -> str:
        command = params.get("command", "")
        if not command:
            raise ValueError("terminal tool requires a 'command' parameter")
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

        output = await loop.run_in_executor(None, _run)
        print(f"   $ {command}\n   {output[:500]}")
        return output

    # ── Mouse ─────────────────────────────────────────────────────────────────

    async def _tool_mouse_move(self, params: dict):
        import pyautogui
        x, y = params["x"], params["y"]
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, lambda: pyautogui.moveTo(x, y, duration=self.settings.mouse_move_duration)
        )

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

        await loop.run_in_executor(None, _click)

    async def _tool_mouse_scroll(self, params: dict):
        import pyautogui
        x = params.get("x", None)
        y = params.get("y", None)
        direction = params.get("direction", "down")
        amount = params.get("amount", 3)
        clicks = amount if direction == "up" else -amount
        loop = asyncio.get_event_loop()

        def _scroll():
            if x is not None and y is not None:
                pyautogui.moveTo(x, y)
            pyautogui.scroll(clicks)
        await loop.run_in_executor(None, _scroll)

    # ── Keyboard ──────────────────────────────────────────────────────────────

    async def _tool_type_text(self, params: dict):
        import pyautogui
        text = params.get("text", "")
        interval = params.get("interval", self.settings.typing_interval)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, lambda: pyautogui.write(text, interval=interval)
        )

    async def _tool_key_press(self, params: dict):
        import pyautogui
        keys = params.get("keys", "")
        loop = asyncio.get_event_loop()

        def _press():
            # Support combos like "ctrl+c", "alt+tab", single keys
            if "+" in keys:
                parts = [k.strip() for k in keys.split("+")]
                pyautogui.hotkey(*parts)
            else:
                pyautogui.press(keys)

        await loop.run_in_executor(None, _press)

    # ── Applications ──────────────────────────────────────────────────────────

    async def _tool_open_app(self, params: dict):
        """Launch an application with proper Windows support + window focus.
        Supports optional 'flags' param, e.g. {"app": "edge", "flags": "--inprivate"}.
        """
        app = params.get("app", "")
        flags = params.get("flags", "")
        if not app:
            raise ValueError("open_app requires an 'app' parameter")

        loop = asyncio.get_event_loop()

        def _open():
            if os.name == "nt":
                alias = _WIN_APP_ALIASES.get(app.lower())
                if alias:
                    cmd = f"{alias} {flags}".strip() if flags else alias
                    # Special handling for Chrome fallback paths
                    if app.lower() in ("chrome", "google chrome"):
                        try:
                            subprocess.Popen(cmd, shell=True)
                        except OSError:
                            _try_chrome_fallbacks(flags)
                    else:
                        subprocess.Popen(cmd, shell=True)
                else:
                    try:
                        cmd = f"{app} {flags}".strip() if flags else app
                        subprocess.Popen(cmd, shell=True)
                    except OSError:
                        subprocess.Popen(f'start "" "{app}"', shell=True)
            elif hasattr(os, "uname") and os.uname().sysname == "Darwin":
                cmd_parts = ["open", "-a", app]
                if flags:
                    cmd_parts += ["--args"] + flags.split()
                subprocess.Popen(cmd_parts)
            else:
                cmd_parts = [app] + (flags.split() if flags else [])
                subprocess.Popen(cmd_parts)

        await loop.run_in_executor(None, _open)

        # Give app time to start and gain focus before subsequent steps
        await asyncio.sleep(1.5)
        await self._focus_window(app)

    async def _focus_window(self, app_name: str):
        """Bring the most recently opened window for app_name to the foreground."""
        if os.name != "nt":
            return
        loop = asyncio.get_event_loop()

        def _focus():
            try:
                import pygetwindow as gw
                # Search by title (partial, case-insensitive)
                search = app_name.lower()
                # Also check the alias target
                alias = _WIN_APP_ALIASES.get(search, "")
                candidates = []
                for win in gw.getAllWindows():
                    title_l = (win.title or "").lower()
                    if search in title_l or (alias and alias.split(".")[-1].replace("exe","").strip() in title_l):
                        candidates.append(win)
                if candidates:
                    w = candidates[0]
                    if w.isMinimized:
                        w.restore()
                    w.activate()
            except ImportError:
                # pygetwindow not installed — skip silently
                pass
            except Exception:
                pass

        await loop.run_in_executor(None, _focus)

    # ── Screenshot & OCR ──────────────────────────────────────────────────────

    async def _tool_screenshot(self, params: dict) -> str:
        """Take a screenshot and save to an actual PNG file. Returns the file path."""
        import pyautogui
        region = params.get("region")  # [x, y, w, h] or None
        loop = asyncio.get_event_loop()

        def _snap() -> str:
            if region:
                img = pyautogui.screenshot(region=tuple(region))
            else:
                img = pyautogui.screenshot()

            # Save to disk as a real PNG
            save_dir = Path.home() / ".jarvis" / "screenshots"
            save_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            filepath = save_dir / f"screenshot_{ts}.png"
            img.save(str(filepath), "PNG")
            return str(filepath)

        path = await loop.run_in_executor(None, _snap)
        print(f"   📸 Screenshot saved: {path}")
        return path

    async def _tool_ocr_read(self, params: dict):
        try:
            import pytesseract
            import pyautogui
        except ImportError:
            return "OCR unavailable. Install: pip install pytesseract pillow"

        region = params.get("region")
        loop = asyncio.get_event_loop()

        def _ocr():
            if region:
                img = pyautogui.screenshot(region=tuple(region))
            else:
                img = pyautogui.screenshot()
            lang = self.settings.ocr_language
            return pytesseract.image_to_string(img, lang=lang)

        text = await loop.run_in_executor(None, _ocr)
        return text.strip()

    # ── Playwright Browser Automation ───────────────────────────────────────────

    async def _ensure_playwright(self):
        """Lazily initialize Playwright browser if not already running."""
        if getattr(ActionExecutor, '_persistent_pw_page', None) is not None:
            try:
                if ActionExecutor._persistent_pw_browser.is_connected():
                    self._pw = ActionExecutor._persistent_pw
                    self._pw_browser = ActionExecutor._persistent_pw_browser
                    self._pw_context = ActionExecutor._persistent_pw_context
                    self._pw_page = ActionExecutor._persistent_pw_page
                    return
            except Exception:
                pass

        if getattr(self, '_pw_page', None) is not None:
            return
        try:
            from playwright.async_api import async_playwright
            self._pw = await async_playwright().start()
            
            try:
                self._pw_browser = await self._pw.chromium.launch(
                    headless=False,
                    slow_mo=0,
                    channel="msedge"
                )
            except Exception:
                self._pw_browser = await self._pw.chromium.launch(
                    headless=False,
                    slow_mo=0
                )
            self._pw_context = await self._pw_browser.new_context()
            self._pw_page = await self._pw_context.new_page()
            self._pw_page.set_default_timeout(self.settings.playwright_timeout)
            
            ActionExecutor._persistent_pw = self._pw
            ActionExecutor._persistent_pw_browser = self._pw_browser
            ActionExecutor._persistent_pw_context = self._pw_context
            ActionExecutor._persistent_pw_page = self._pw_page
            
            self._pw_browser_persistent = True
            
            print("🎭 Playwright browser launched.")
        except ImportError:
            print("⚠️  playwright not installed. Run: pip install playwright && playwright install")
            self._pw_page = None
            raise
        except Exception as e:
            print(f"⚠️  Playwright launch failed: {e}")
            self._pw_page = None
            raise

    async def _cleanup_playwright(self, force=False):
        """Close Playwright browser and stop the engine."""
        if getattr(self, '_pw_browser_persistent', False) and not force:
            return

        if getattr(ActionExecutor, '_persistent_pw_browser', None):
            try:
                await ActionExecutor._persistent_pw_browser.close()
            except Exception:
                pass
        if getattr(ActionExecutor, '_persistent_pw', None):
            try:
                await ActionExecutor._persistent_pw.stop()
            except Exception:
                pass
        self._pw_page = None
        self._pw_browser = None
        self._pw_context = None
        self._pw = None
        
        ActionExecutor._persistent_pw_page = None
        ActionExecutor._persistent_pw_browser = None
        ActionExecutor._persistent_pw_context = None
        ActionExecutor._persistent_pw = None

    async def _tool_browser_open(self, params: dict):
        url = params.get("url", "")
        if not url:
            raise ValueError("browser_open requires a 'url' parameter")

        await self._ensure_playwright()
        await self._pw_page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await self._pw_page.wait_for_timeout(2000)
        print(f"   🎭 Navigated to: {url}")

    async def _tool_browser_search(self, params: dict):
        query = params.get("query", "")
        engine = params.get("engine", "google")
        
        await self._ensure_playwright()
        
        current_url = self._pw_page.url
        if "youtube.com" in current_url:
            selectors = [
                ('input[name="search_query"]', 3000),
                ('ytd-searchbox input', 3000),
                ('input#search', 15000)
            ]
            clicked = False
            for selector, t in selectors:
                try:
                    await self._pw_page.click(selector, timeout=t)
                    await self._pw_page.fill(selector, query, timeout=t)
                    await self._pw_page.press(selector, "Enter", timeout=t)
                    print(f"   🎭 Searched YouTube for: {query}")
                    clicked = True
                    break
                except Exception:
                    continue
            
            if clicked:
                return self._pw_page.url
            else:
                print("   ⚠️  Failed to find YouTube search box.")

        urls = {
            "google": f"https://www.google.com/search?q={query.replace(' ', '+')}",
            "duckduckgo": f"https://duckduckgo.com/?q={query.replace(' ', '+')}",
        }
        url = urls.get(engine, urls["google"])
        await self._tool_browser_open({"url": url})
        return url

    async def _tool_browser_click(self, params: dict):
        """Click an element on the Playwright page. params: {selector: str}"""
        selector = params.get("selector", "")
        if not selector:
            raise ValueError("browser_click requires a 'selector' parameter")
        await self._ensure_playwright()
        await self._pw_page.click(selector)
        print(f"   🎭 Clicked: {selector}")

    async def _tool_browser_type(self, params: dict):
        """Type text into an element. params: {selector: str, text: str}"""
        selector = params.get("selector", "")
        text = params.get("text", "")
        if not selector:
            raise ValueError("browser_type requires a 'selector' parameter")
        await self._ensure_playwright()
        await self._pw_page.fill(selector, text)
        print(f"   🎭 Typed into {selector}: {text[:50]}")

    async def _tool_browser_navigate(self, params: dict):
        """Navigate the Playwright page to a URL. params: {url: str}"""
        url = params.get("url", "")
        if not url:
            raise ValueError("browser_navigate requires a 'url' parameter")
        await self._ensure_playwright()
        await self._pw_page.goto(url, wait_until="networkidle", timeout=30000)
        await self._pw_page.wait_for_timeout(2000)
        print(f"   🎭 Navigated to: {url}")

    async def _tool_browser_extract(self, params: dict) -> str:
        """Extract text or attribute from an element.
        params: {selector: str, attribute: str (optional)}
        """
        selector = params.get("selector", "")
        attribute = params.get("attribute", "")
        if not selector:
            raise ValueError("browser_extract requires a 'selector' parameter")
        await self._ensure_playwright()
        element = await self._pw_page.query_selector(selector)
        if element is None:
            raise ValueError(f"Element not found: {selector}")
        if attribute:
            value = await element.get_attribute(attribute)
            return value or ""
        text = await element.text_content()
        return (text or "").strip()

    # ── Files ─────────────────────────────────────────────────────────────────

    async def _tool_file_read(self, params: dict) -> str:
        path = _resolve_path(params.get("path", ""))
        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")
        loop = asyncio.get_event_loop()

        def _read():
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()

        return await loop.run_in_executor(None, _read)

    async def _tool_file_write(self, params: dict):
        path = _resolve_path(params.get("path", ""))
        content = params.get("content", "")
        mode = params.get("mode", "w")
        loop = asyncio.get_event_loop()

        def _write():
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, mode, encoding="utf-8") as f:
                f.write(content)

        await loop.run_in_executor(None, _write)

    async def _tool_file_delete(self, params: dict):
        import shutil
        path = _resolve_path(params.get("path", ""))
        if not path.exists():
            raise FileNotFoundError(f"Cannot delete — path does not exist: {path}")
        loop = asyncio.get_event_loop()

        def _delete():
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()

        await loop.run_in_executor(None, _delete)

    async def _tool_create_folder(self, params: dict):
        """Create a directory (and parents). params: {path: str}"""
        path = _resolve_path(params.get("path", ""))
        if not str(path) or str(path) == ".":
            raise ValueError("create_folder requires a 'path' parameter")
        loop = asyncio.get_event_loop()

        def _mkdir():
            path.mkdir(parents=True, exist_ok=True)

        await loop.run_in_executor(None, _mkdir)
        print(f"   📁 Folder created: {path}")

    # ── Clipboard ─────────────────────────────────────────────────────────────

    async def _tool_clipboard_copy(self, params: dict):
        try:
            import pyperclip
            text = params.get("text", "")
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: pyperclip.copy(text))
        except ImportError:
            print("⚠️  pyperclip not installed.")

    async def _tool_clipboard_paste(self, params: dict) -> str:
        try:
            import pyperclip
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, pyperclip.paste)
        except ImportError:
            return ""

    # ── TTS / Notifications ───────────────────────────────────────────────────

    async def speak(self, text: str):
        """Speak text using configured TTS provider."""
        await self._tool_speak({"text": text})

    async def _tool_speak(self, params: dict):
        text = params.get("text", "")
        provider = self.settings.tts_provider
        loop = asyncio.get_event_loop()

        if provider == "pyttsx3" or provider == "edge-tts":
            async def _say():
                try:
                    import edge_tts
                    import tempfile
                    import os
                    import playsound3 as playsound
                    
                    communicate = edge_tts.Communicate(text, "en-US-GuyNeural")
                    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                        tmp_name = f.name
                        
                    await communicate.save(tmp_name)
                    
                    try:
                        await loop.run_in_executor(None, playsound.playsound, tmp_name)
                    finally:
                        try:
                            os.unlink(tmp_name)
                        except:
                            pass
                except ImportError:
                    print(f"🔈 {text} (Install edge-tts and playsound3 to enable voice)")
                    
            await _say()

        elif provider == "elevenlabs":
            await self._elevenlabs_speak(text)

        elif provider == "gtts":
            def _gtts():
                from gtts import gTTS
                import tempfile, playsound  # noqa: E401
                tts = gTTS(text=text, lang="en")
                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                    tts.save(f.name)
                    playsound.playsound(f.name)
            await loop.run_in_executor(None, _gtts)

        else:
            print(f"🔈 {text}")

    async def _elevenlabs_speak(self, text: str):
        import httpx
        import tempfile
        import playsound3 as playsound
        api_key = self.settings.tts_elevenlabs_key
        voice_id = self.settings.tts_elevenlabs_voice_id or "21m00Tcm4TlvDq8ikWAM"
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                headers={"xi-api-key": api_key},
                json={"text": text, "model_id": "eleven_monolingual_v1"},
            )
            resp.raise_for_status()
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                f.write(resp.content)
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, lambda: playsound.playsound(f.name))

    async def _tool_notify(self, params: dict):
        title = params.get("title", "JARVIS")
        message = params.get("message", "")
        try:
            from plyer import notification
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: notification.notify(title=title, message=message, timeout=5),
            )
        except ImportError:
            print(f"🔔 {title}: {message}")

    async def _tool_wait(self, params: dict):
        seconds = float(params.get("seconds", 1.0))
        await asyncio.sleep(seconds)

