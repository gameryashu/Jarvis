"""
core/executor.py — Action execution engine.
Dispatches ActionStep objects to the correct tool handler.
Controls mouse, keyboard, terminal, browser, files, clipboard, TTS, and more.
"""

import asyncio
import logging
import os
import subprocess
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from config.settings import Settings
from core.llm import ActionStep

logger = logging.getLogger(__name__)

# Common application aliases for Windows
APP_ALIASES: dict[str, str] = {
    "calculator": "calc.exe",
    "calc": "calc.exe",
    "notepad": "notepad.exe",
    "paint": "mspaint.exe",
    "explorer": "explorer.exe",
    "cmd": "cmd.exe",
    "powershell": "powershell.exe",
    "word": "winword.exe",
    "excel": "excel.exe",
    "chrome": "chrome",
    "edge": "msedge",
    "firefox": "firefox",
    "spotify": "spotify",
    "discord": "discord",
    "vlc": "vlc",
    "vscode": "code",
    "vs code": "code",
    "visual studio code": "code",
    "task manager": "taskmgr.exe",
    "control panel": "control.exe",
    "settings": "ms-settings:",
    "snipping tool": "snippingtool.exe",
}


@dataclass
class ExecutionResult:
    success: bool
    output: Any = None
    error: str = ""
    step: Optional[ActionStep] = None


class ActionExecutor:
    """
    Routes action steps to tool handlers.
    Each handler is an async method named _tool_<tool_name>.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        # Playwright persistent state
        self._playwright = None
        self._browser = None
        self._persistent_page = None

    # ── Dispatcher ────────────────────────────────────────────────────────────

    async def execute(self, step: ActionStep) -> ExecutionResult:
        """Dispatch a step to the appropriate tool handler."""
        if step.is_destructive and self.settings.confirm_destructive:
            confirmed = await self._confirm(step)
            if not confirmed:
                return ExecutionResult(
                    success=False,
                    error="User declined confirmation.",
                    step=step,
                )

        handler_name = f"_tool_{step.tool}"
        handler = getattr(self, handler_name, None)

        if handler is None:
            return ExecutionResult(
                success=False,
                error=f"Unknown tool: {step.tool}",
                step=step,
            )

        try:
            result = await handler(step.params)
            return ExecutionResult(success=True, output=result, step=step)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Tool %s failed: %s", step.tool, e, exc_info=True)
            return ExecutionResult(success=False, error=str(e), step=step)

    async def _confirm(self, step: ActionStep) -> bool:
        """Ask user to confirm destructive action."""
        print(f"\n⚠️  CONFIRM: {step.description}")
        print(f"   Tool: {step.tool}, Params: {step.params}")
        loop = asyncio.get_event_loop()
        answer = await loop.run_in_executor(None, input, "   Proceed? [y/N] ")
        return answer.strip().lower() in ("y", "yes")

    # ── Playwright Management ─────────────────────────────────────────────────

    async def _ensure_playwright(self):
        """
        Ensure a live Playwright browser page is available.
        If the current page is stale (browser crash / user closed window),
        reset all references and relaunch.
        """
        # Health-check existing page
        if self._persistent_page is not None:
            try:
                await self._persistent_page.evaluate("1+1")
                return  # Page is healthy
            except Exception:
                logger.warning("Playwright page is stale — relaunching browser.")
                self._playwright = None
                self._browser = None
                self._persistent_page = None

        # Launch fresh
        try:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=False,
                slow_mo=0,
                args=["--start-maximized"],
            )
            context = await self._browser.new_context(no_viewport=True)
            self._persistent_page = await context.new_page()
        except Exception as e:
            self._playwright = None
            self._browser = None
            self._persistent_page = None
            raise RuntimeError(f"Failed to launch Playwright browser: {e}") from e

    async def close(self):
        """Cleanly tear down Playwright."""
        try:
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception as e:
            logger.warning("Error closing Playwright: %s", e)
        finally:
            self._playwright = None
            self._browser = None
            self._persistent_page = None

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
        x = params.get("x")
        y = params.get("y")
        direction = params.get("direction", "down")
        amount = params.get("amount", 3)
        clicks = amount if direction == "up" else -amount
        loop = asyncio.get_event_loop()

        def _scroll():
            if x and y:
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
            if "+" in keys:
                parts = [k.strip() for k in keys.split("+")]
                pyautogui.hotkey(*parts)
            else:
                pyautogui.press(keys)

        await loop.run_in_executor(None, _press)

    # ── Applications ──────────────────────────────────────────────────────────

    async def _tool_open_app(self, params: dict):
        app = params.get("app", "").strip()
        flags = params.get("flags", "")
        loop = asyncio.get_event_loop()

        # Resolve alias
        resolved = APP_ALIASES.get(app.lower(), app)

        def _open():
            if os.name == "nt":
                # Handle ms-settings: and similar URI schemes
                if ":" in resolved and not resolved.endswith(".exe"):
                    os.startfile(resolved)
                    return
                cmd = [resolved]
                if flags:
                    cmd += flags.split()
                try:
                    subprocess.Popen(cmd, shell=True)
                except FileNotFoundError:
                    # Fallback: try os.startfile for registered apps
                    os.startfile(resolved)
            else:
                subprocess.Popen([resolved])

        await loop.run_in_executor(None, _open)
        await asyncio.sleep(0.5)  # Let window appear

    # ── Screenshot & OCR ──────────────────────────────────────────────────────

    async def _tool_screenshot(self, params: dict):
        import pyautogui
        region = params.get("region")
        loop = asyncio.get_event_loop()

        def _snap():
            if region:
                return pyautogui.screenshot(region=tuple(region))
            return pyautogui.screenshot()

        return await loop.run_in_executor(None, _snap)

    async def _tool_ocr_read(self, params: dict):
        try:
            import pytesseract
            import pyautogui
        except ImportError:
            return "OCR unavailable. Install: pip install pytesseract pillow"

        region = params.get("region")
        loop = asyncio.get_event_loop()

        def _ocr():
            try:
                if region:
                    img = pyautogui.screenshot(region=tuple(region))
                else:
                    img = pyautogui.screenshot()
                lang = self.settings.ocr_language
                return pytesseract.image_to_string(img, lang=lang)
            except Exception as e:
                return f"OCR unavailable: {e}"

        text = await loop.run_in_executor(None, _ocr)
        return text.strip()

    # ── Browser ───────────────────────────────────────────────────────────────

    async def _tool_browser_open(self, params: dict):
        """Open a URL using the system default browser (not Playwright)."""
        url = params.get("url", "")
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: webbrowser.open(url))

    async def _tool_web_search(self, params: dict):
        """Search the web using the default browser."""
        query = params.get("query", "")
        url = f"https://www.google.com/search?q={query.replace(' ', '+')}"
        import webbrowser
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: webbrowser.open(url))
        return url


    # ── Media Tools ───────────────────────────────────────────────────────────

    async def _tool_play_youtube(self, params: dict) -> str:
        """
        Navigate to YouTube search results and play the first real video.
        Uses Playwright JS evaluation to extract video href (bypasses Shadow DOM).
        Falls back to system browser if Playwright fails.
        """
        query = params.get("query", "")
        encoded = urllib.parse.quote_plus(query)
        search_url = f"https://www.youtube.com/results?search_query={encoded}"

        try:
            await self._ensure_playwright()
            page = self._persistent_page

            print(f"   🎬 Navigating to YouTube search: {query}")
            # Wait networkidle
            await page.goto(search_url, wait_until="networkidle", timeout=30000)
            
            href = None
            for attempt in range(3):
                await asyncio.sleep(3)
                href = await page.evaluate("""
                    () => {
                        const items = document.querySelectorAll('ytd-video-renderer');
                        for (const item of items) {
                            const a = item.querySelector('a#video-title');
                            if (a && a.href && a.href.includes('/watch')) return a.href;
                        }
                        return null;
                    }
                """)
                if href:
                    break
                print(f"   ⏳ Retrying YouTube JS extraction (attempt {attempt + 2}/3)")

            if href:
                print(f"   ▶️  Playing: {href}")
                await page.goto(href, wait_until="domcontentloaded", timeout=30000)
                return f"Playing YouTube: {href}"
            else:
                # Fallback: open search in system browser
                logger.warning("Could not extract video href — falling back to system browser.")
                import webbrowser
                webbrowser.open(search_url)
                return f"Opened YouTube search in browser: {query}"

        except Exception as e:
            logger.error("play_youtube failed: %s", e)
            import webbrowser
            # Hard fallback
            webbrowser.open(search_url)
            return f"Opened YouTube search (fallback): {query}"

    async def _tool_play_spotify(self, params: dict) -> str:
        """Open Spotify and search for a query using the spotify: URI scheme."""
        query = params.get("query", "")
        encoded = query.replace(" ", "%20")
        uri = f"spotify:search:{encoded}"
        loop = asyncio.get_event_loop()

        def _open():
            subprocess.Popen(f'start "" "{uri}"', shell=True)

        await loop.run_in_executor(None, _open)
        return f"Opened Spotify search: {query}"

    async def _tool_browser_click(self, params: dict) -> str:
        """Click an element inside a Playwright-managed browser page."""
        selector = params.get("selector", "")
        await self._ensure_playwright()
        page = self._persistent_page
        await page.click(selector, timeout=10000)
        return f"Clicked: {selector}"

    async def _tool_browser_type(self, params: dict) -> str:
        """Type text into an element inside the Playwright-managed browser."""
        selector = params.get("selector", "")
        text = params.get("text", "")
        await self._ensure_playwright()
        page = self._persistent_page
        await page.fill(selector, text)
        return f"Typed into {selector}"

    async def _tool_browser_goto(self, params: dict) -> str:
        """Navigate the Playwright browser to a URL."""
        url = params.get("url", "")
        await self._ensure_playwright()
        page = self._persistent_page
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        return f"Navigated to: {url}"

    # ── Files ─────────────────────────────────────────────────────────────────

    async def _tool_file_read(self, params: dict) -> str:
        path = Path(params.get("path", "")).expanduser()
        loop = asyncio.get_event_loop()

        def _read():
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()

        return await loop.run_in_executor(None, _read)

    async def _tool_file_write(self, params: dict):
        path = Path(params.get("path", "")).expanduser()
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
        path = Path(params.get("path", "")).expanduser()
        loop = asyncio.get_event_loop()

        def _delete():
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()

        await loop.run_in_executor(None, _delete)

    async def _tool_create_folder(self, params: dict):
        path = Path(params.get("path", "")).expanduser()
        loop = asyncio.get_event_loop()
        def _create():
            path.mkdir(parents=True, exist_ok=True)
            return str(path)
        await loop.run_in_executor(None, _create)

    # ── Clipboard ─────────────────────────────────────────────────────────────

    async def _tool_clipboard_copy(self, params: dict):
        try:
            import pyperclip
            text = params.get("text", "")
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: pyperclip.copy(text))
        except ImportError:
            logger.warning("pyperclip not installed.")

    async def _tool_clipboard_paste(self, params: dict) -> str:
        try:
            import pyperclip
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, pyperclip.paste)
        except ImportError:
            return ""

    # ── Advanced Features ─────────────────────────────────────────────────────

    async def _tool_analyze_screen(self, params: dict) -> dict:
        import pyautogui
        try:
            import pytesseract
        except ImportError:
            pytesseract = None
        try:
            import pygetwindow as gw
        except ImportError:
            gw = None

        loop = asyncio.get_event_loop()

        def _analyze():
            screenshot = pyautogui.screenshot()
            text = pytesseract.image_to_string(screenshot) if pytesseract else "OCR unavailable."
            active = gw.getActiveWindow() if gw else None
            app_name = active.title if active else ""
            return {
                "url": "",
                "app_name": app_name,
                "visible_text": text.strip()[:1000],
                "window_title": app_name
            }
        return await loop.run_in_executor(None, _analyze)

    async def _tool_run_code(self, params: dict):
        code = params.get("code", "")
        language = params.get("language", "python")
        import tempfile, subprocess, sys
        loop = asyncio.get_event_loop()
        def _run():
            with tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w", encoding="utf-8") as f:
                f.write(code)
                tmp = f.name
            try:
                result = subprocess.run([sys.executable, tmp], capture_output=True, text=True, timeout=60)
                return (result.stdout + "\n" + result.stderr).strip()
            except Exception as e:
                return f"Execution error: {e}"
            finally:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
        return await loop.run_in_executor(None, _run)

    async def _tool_focus_window(self, params: dict):
        title = params.get("title", "")
        loop = asyncio.get_event_loop()
        def _focus():
            try:
                import pygetwindow as gw
            except ImportError:
                return "pygetwindow not installed."
            windows = gw.getWindowsWithTitle(title)
            if windows:
                win = windows[0]
                try:
                    win.restore()
                    win.activate()
                except Exception:
                    pass
                return f"Focused window: {win.title}"
            return f"Window containing '{title}' not found."
        return await loop.run_in_executor(None, _focus)

    async def _tool_system_info(self, params: dict):
        metric = params.get("metric", "").lower()
        loop = asyncio.get_event_loop()
        def _info():
            try:
                import psutil
            except ImportError:
                return "psutil not installed."
            if "cpu" in metric:
                return f"CPU usage: {psutil.cpu_percent(interval=1)}%"
            elif "ram" in metric or "memory" in metric:
                vm = psutil.virtual_memory()
                return f"RAM usage: {vm.percent}% ({vm.used / (1024**3):.1f}GB / {vm.total / (1024**3):.1f}GB)"
            elif "disk" in metric:
                disk = psutil.disk_usage('/')
                return f"Disk usage: {disk.percent}% ({disk.free / (1024**3):.1f}GB free)"
            elif "battery" in metric:
                if hasattr(psutil, "sensors_battery") and psutil.sensors_battery():
                    batt = psutil.sensors_battery()
                    return f"Battery: {batt.percent}% (Plugged in: {batt.power_plugged})"
                return "Battery info not available."
            return f"Unsupported metric: {metric}. Try cpu, ram, disk, or battery."
        return await loop.run_in_executor(None, _info)

    # ── TTS / Notifications ───────────────────────────────────────────────────

    async def speak(self, text: str):
        """Speak text using configured TTS provider."""
        await self._tool_speak({"text": text})

    async def _tool_speak(self, params: dict):
        text = params.get("text", "")
        loop = asyncio.get_event_loop()

        try:
            import edge_tts
            import tempfile

            communicate = edge_tts.Communicate(text, "en-US-GuyNeural")
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                tmp_name = f.name

            await communicate.save(tmp_name)

            try:
                import playsound
                await loop.run_in_executor(None, playsound.playsound, tmp_name)
            except ImportError:
                # playsound not available — try pygame as fallback
                try:
                    import pygame
                    pygame.mixer.init()
                    pygame.mixer.music.load(tmp_name)
                    pygame.mixer.music.play()
                    while pygame.mixer.music.get_busy():
                        await asyncio.sleep(0.1)
                except ImportError:
                    print(f"🔈 {text}")
            finally:
                try:
                    os.unlink(tmp_name)
                except OSError as e:
                    logger.warning("Could not delete TTS temp file: %s", e)

        except ImportError:
            # edge-tts not available — pyttsx3 fallback
            try:
                import pyttsx3
                def _say():
                    engine = pyttsx3.init()
                    engine.say(text)
                    engine.runAndWait()
                await loop.run_in_executor(None, _say)
            except ImportError:
                print(f"🔈 {text}")

    async def _elevenlabs_speak(self, text: str):
        import httpx
        import tempfile
        import playsound
        api_key = self.settings.tts_elevenlabs_key
        voice_id = self.settings.tts_elevenlabs_voice_id or "21m00Tcm4TlvDq8ikWAM"
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                headers={"xi-api-key": api_key},
                json={"text": text, "model_id": "eleven_monolingual_v1"},
            )
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                f.write(resp.content)
                tmp_name = f.name
            loop = asyncio.get_event_loop()
            try:
                await loop.run_in_executor(None, lambda: playsound.playsound(tmp_name))
            finally:
                try:
                    os.unlink(tmp_name)
                except OSError as e:
                    logger.warning("Could not delete ElevenLabs temp file: %s", e)

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
