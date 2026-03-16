import os

code = """
import asyncio
import os
import subprocess
import pathlib
import time
import shutil
import base64
import io
import json
import datetime
import typing
import dataclasses
import re

def _log_action(tool: str, params: dict, success: bool, detail: str = "") -> None:
    try:
        log_dir = pathlib.Path.home() / ".jarvis" / "memory"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "action_log.txt"
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        status = "OK" if success else "FAIL"
        param_str = str(params)[:120]
        detail_str = (detail or "")[:200]
        line = f"[{ts}] [{status}] tool={tool} params={param_str} detail={detail_str}\\n"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass

_WIN_APP_ALIASES = {
    "calculator": "calc",
    "calc": "calc",
    "notepad": "notepad",
    "paint": "mspaint",
    "chrome": "start msedge",
    "google chrome": "start msedge",
    "edge": "start msedge",
    "microsoft edge": "start msedge",
    "explorer": "explorer",
    "cmd": "cmd",
    "powershell": "powershell",
    "terminal": "wt",
    "vs code": "code",
    "vscode": "code",
    "antigravity": "antigravity",
    "spotify": "start spotify:",
    "task manager": "taskmgr",
    "snipping tool": "snippingtool"
}

@dataclasses.dataclass
class ExecutionResult:
    success: bool
    output: typing.Any = None
    error: str = ""
    step: typing.Any = None

class ActionExecutor:
    _persistent_pw = None
    _persistent_browser = None
    _persistent_page = None

    def __init__(self, settings: typing.Any):
        self.settings = settings
        self._tts_engine = None

    async def execute(self, step: typing.Any, _retry: bool = True) -> ExecutionResult:
        if getattr(step, "is_destructive", False) and getattr(self.settings, "confirm_destructive", False):
            try:
                confirmed = await self._confirm(step)
            except (asyncio.CancelledError, Exception) as e:
                return ExecutionResult(success=False, error=f"Confirmation error: {e}", step=step)
            if not confirmed:
                _log_action(getattr(step, "tool", ""), getattr(step, "params", {}), False, "User declined confirmation")
                return ExecutionResult(success=False, error="User declined confirmation.", step=step)

        handler_name = f"_tool_{getattr(step, 'tool', '')}"
        handler = getattr(self, handler_name, None)

        if handler is None:
            _log_action(getattr(step, "tool", ""), getattr(step, "params", {}), False, f"Unknown tool")
            return ExecutionResult(success=False, error=f"Unknown tool", step=step)

        max_attempts = 2 if _retry else 1
        for attempt in range(max_attempts):
            try:
                result = await handler(getattr(step, "params", {}))
                _log_action(getattr(step, "tool", ""), getattr(step, "params", {}), True, str(result)[:200] if result is not None else "")
                return ExecutionResult(success=True, output=result, step=step)
            except (asyncio.CancelledError, Exception) as e:
                if attempt == 0 and max_attempts > 1:
                    try:
                        await asyncio.sleep(0.5)
                    except (asyncio.CancelledError, Exception):
                        pass
                else:
                    err = f"{type(e).__name__}: {e}"
                    _log_action(getattr(step, "tool", ""), getattr(step, "params", {}), False, err)
                    return ExecutionResult(success=False, error=err, step=step)

        return ExecutionResult(success=False, error="Unexpected retry exhaustion", step=step)

    async def _confirm(self, step: typing.Any) -> bool:
        try:
            print(f"\\n⚠️  CONFIRM: {getattr(step, 'description', '')}")
            loop = asyncio.get_event_loop()
            answer = await loop.run_in_executor(None, input, "   Proceed? [y/N] ")
            return answer.strip().lower() in ("y", "yes")
        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"Confirm error: {e}") from e

    async def _tool_terminal(self, params: dict) -> str:
        try:
            command = params.get("command", "")
            loop = asyncio.get_event_loop()
            def _run():
                result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=60)
                return (result.stdout + result.stderr).strip()
            return await loop.run_in_executor(None, _run)
        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"Terminal error: {e}") from e

    async def _tool_mouse_move(self, params: dict):
        try:
            import pyautogui
            x, y = params.get("x", 0), params.get("y", 0)
            duration = getattr(self.settings, "mouse_move_duration", 0.0)
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: pyautogui.moveTo(x, y, duration=duration))
        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"mouse_move error: {e}") from e

    async def _tool_mouse_click(self, params: dict):
        try:
            import pyautogui
            x, y = params.get("x"), params.get("y")
            button = params.get("button", "left")
            duration = getattr(self.settings, "mouse_move_duration", 0.0)
            loop = asyncio.get_event_loop()
            def _click():
                if x is not None and y is not None:
                    pyautogui.moveTo(x, y, duration=duration)
                if button == "double":
                    pyautogui.doubleClick()
                elif button == "right":
                    pyautogui.rightClick()
                else:
                    pyautogui.click()
            await loop.run_in_executor(None, _click)
        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"mouse_click error: {e}") from e

    async def _tool_mouse_scroll(self, params: dict):
        try:
            import pyautogui
            x, y = params.get("x"), params.get("y")
            direction = params.get("direction", "down")
            amount = params.get("amount", 3)
            clicks = amount if direction == "up" else -amount
            loop = asyncio.get_event_loop()
            def _scroll():
                if x is not None and y is not None:
                    pyautogui.moveTo(x, y)
                pyautogui.scroll(clicks)
            await loop.run_in_executor(None, _scroll)
        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"mouse_scroll error: {e}") from e

    async def _tool_type_text(self, params: dict):
        try:
            import pyautogui
            text = params.get("text", "")
            interval = getattr(self.settings, "typing_interval", 0.0)
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: pyautogui.write(text, interval=interval))
        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"type_text error: {e}") from e

    async def _tool_key_press(self, params: dict):
        try:
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
        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"key_press error: {e}") from e

    async def _tool_open_app(self, params: dict):
        try:
            app = params.get("app", "").strip().lower()
            flags = params.get("flags", "").strip()
            cmd = _WIN_APP_ALIASES.get(app, app)
            if flags:
                cmd = f"{cmd} {flags}"
            loop = asyncio.get_event_loop()
            def _open():
                subprocess.Popen(cmd, shell=True)
                time.sleep(0.5)
            await loop.run_in_executor(None, _open)
        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"open_app error: {e}") from e

    async def _tool_screenshot(self, params: dict) -> str:
        try:
            import pyautogui
            region = params.get("region")
            loop = asyncio.get_event_loop()
            def _snap() -> str:
                save_dir = pathlib.Path.home() / ".jarvis" / "screenshots"
                save_dir.mkdir(parents=True, exist_ok=True)
                filename = f"screenshot_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.png"
                save_path = save_dir / filename
                if region:
                    img = pyautogui.screenshot(region=tuple(region))
                else:
                    img = pyautogui.screenshot()
                img.save(str(save_path))
                return str(save_path)
            return await loop.run_in_executor(None, _snap)
        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"screenshot error: {e}") from e

    async def _tool_ocr_read(self, params: dict) -> str:
        try:
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
                lang = getattr(self.settings, "ocr_language", "eng")
                try:
                    return pytesseract.image_to_string(img, lang=lang).strip()
                except Exception:
                    return ""
            return await loop.run_in_executor(None, _ocr)
        except (asyncio.CancelledError, Exception):
            return ""

    async def _ensure_playwright(self):
        try:
            if ActionExecutor._persistent_page is not None:
                try:
                    _ = ActionExecutor._persistent_page.url
                    return ActionExecutor._persistent_page
                except Exception:
                    ActionExecutor._persistent_page = None
                    ActionExecutor._persistent_browser = None
                    ActionExecutor._persistent_pw = None

            from playwright.async_api import async_playwright
            pw = await async_playwright().start()
            ActionExecutor._persistent_pw = pw

            try:
                browser = await pw.chromium.launch(channel="msedge", headless=False, slow_mo=0)
            except Exception:
                browser = await pw.chromium.launch(headless=False, slow_mo=0)

            ActionExecutor._persistent_browser = browser
            page = await browser.new_page()
            page.set_default_timeout(30_000)
            ActionExecutor._persistent_page = page
            return page
        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"Failed to start Playwright: {e}") from e

    async def _cleanup_playwright(self, force: bool = False) -> None:
        try:
            if not force:
                return
            if ActionExecutor._persistent_browser is not None:
                await ActionExecutor._persistent_browser.close()
            if ActionExecutor._persistent_pw is not None:
                await ActionExecutor._persistent_pw.stop()
        except (asyncio.CancelledError, Exception):
            pass
        finally:
            if force:
                ActionExecutor._persistent_page = None
                ActionExecutor._persistent_browser = None
                ActionExecutor._persistent_pw = None

    async def _tool_browser_open(self, params: dict) -> str:
        try:
            url = params.get("url", "")
            page = await self._ensure_playwright()
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(2.0)
            return page.url
        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"browser_open error: {e}") from e

    async def _tool_browser_navigate(self, params: dict) -> str:
        try:
            url = params.get("url", "")
            page = await self._ensure_playwright()
            await page.goto(url, wait_until="networkidle", timeout=30_000)
            await asyncio.sleep(2.0)
            return page.url
        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"browser_navigate error: {e}") from e

    async def _tool_browser_search(self, params: dict) -> str:
        try:
            query = params.get("query", "")
            page = await self._ensure_playwright()
            current_url = page.url

            if "youtube.com" in current_url:
                selectors = ["input[name='search_query']", "ytd-searchbox input", "input#search"]
                success = False
                for sel in selectors:
                    try:
                        search_input = page.locator(sel).first
                        await search_input.wait_for(state="visible", timeout=15_000)
                        await search_input.fill(query)
                        await search_input.press("Enter")
                        success = True
                        break
                    except (asyncio.CancelledError, Exception):
                        continue
                if not success:
                    encoded = query.replace(" ", "+")
                    google_url = f"https://www.google.com/search?q={encoded}"
                    await page.goto(google_url, wait_until="networkidle", timeout=30_000)
            else:
                encoded = query.replace(" ", "+")
                google_url = f"https://www.google.com/search?q={encoded}"
                await page.goto(google_url, wait_until="networkidle", timeout=30_000)

            return page.url
        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"browser_search error: {e}") from e

    async def _tool_browser_click(self, params: dict) -> str:
        try:
            selector = params.get("selector", "")
            page = await self._ensure_playwright()
            await page.click(selector, timeout=15_000)
            return f"Clicked: {selector}"
        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"browser_click error: {e}") from e

    async def _tool_browser_type(self, params: dict) -> str:
        try:
            selector = params.get("selector", "")
            text = params.get("text", "")
            page = await self._ensure_playwright()
            await page.fill(selector, text, timeout=15_000)
            return f"Typed into: {selector}"
        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"browser_type error: {e}") from e

    async def _tool_browser_extract(self, params: dict) -> str:
        try:
            selector = params.get("selector", "")
            page = await self._ensure_playwright()
            el = page.locator(selector).first
            result = await el.inner_text(timeout=15_000)
            return result or ""
        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"browser_extract error: {e}") from e

    def _resolve_path(self, raw: str) -> pathlib.Path:
        p = raw.strip()
        if p.startswith("~/Desktop") or p.startswith("~\\\\Desktop"):
            rest = p[len("~/Desktop"):].lstrip("/\\\\")
            base = pathlib.Path.home() / "Desktop"
            return base / rest if rest else base
        return pathlib.Path(p).expanduser()

    async def _tool_file_read(self, params: dict) -> str:
        try:
            path = self._resolve_path(params.get("path", ""))
            loop = asyncio.get_event_loop()
            def _read():
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    return f.read()
            return await loop.run_in_executor(None, _read)
        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"file_read error: {e}") from e

    async def _tool_file_write(self, params: dict):
        try:
            path = self._resolve_path(params.get("path", ""))
            content = params.get("content", "")
            mode = params.get("mode", "w")
            loop = asyncio.get_event_loop()
            def _write():
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, mode, encoding="utf-8") as f:
                    f.write(content)
            await loop.run_in_executor(None, _write)
        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"file_write error: {e}") from e

    async def _tool_file_delete(self, params: dict):
        try:
            path_str = params.get("path", "")
            # Requirement: folder_name = params path split on / and \ take last part,
            # full_path = pathlib.Path.home()/"Desktop"/folder_name
            parts = path_str.replace("\\\\", "/").split("/")
            folder_name = parts[-1] if parts else ""
            if folder_name:
                path = pathlib.Path.home() / "Desktop" / folder_name
            else:
                path = pathlib.Path.home() / "Desktop"

            loop = asyncio.get_event_loop()
            def _delete():
                if path.is_dir():
                    shutil.rmtree(path)
                elif path.exists():
                    path.unlink()
            await loop.run_in_executor(None, _delete)
        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"file_delete error: {e}") from e

    async def _tool_create_folder(self, params: dict) -> str:
        try:
            path_str = params.get("path", params.get("name", ""))
            parts = path_str.replace("\\\\", "/").split("/")
            folder_name = parts[-1] if parts else ""
            if folder_name:
                path = pathlib.Path.home() / "Desktop" / folder_name
            else:
                path = pathlib.Path.home() / "Desktop"

            loop = asyncio.get_event_loop()
            def _make() -> str:
                path.mkdir(parents=True, exist_ok=True)
                return str(path)
            return await loop.run_in_executor(None, _make)
        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"create_folder error: {e}") from e

    async def _tool_clipboard_copy(self, params: dict):
        try:
            try:
                import pyperclip
            except ImportError:
                return
            text = params.get("text", "")
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: pyperclip.copy(text))
        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"clipboard_copy error: {e}") from e

    async def _tool_clipboard_paste(self, params: dict) -> str:
        try:
            try:
                import pyperclip
            except ImportError:
                return ""
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, pyperclip.paste)
        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"clipboard_paste error: {e}") from e

    async def _tool_speak(self, params: dict):
        try:
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
                        tmp_path = pathlib.Path(f.name)
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
                    print(f"🔈 {text}")
            try:
                await _edge_tts_speak()
            except asyncio.CancelledError:
                print(f"🔈 {text}")
                raise
        except (asyncio.CancelledError, Exception) as e:
            print(f"🔈 {params.get('text', '')}")

    async def _tool_wait(self, params: dict):
        try:
            seconds = float(params.get("seconds", 1.0))
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            raise RuntimeError(f"wait error: {e}") from e

    async def _tool_notify(self, params: dict):
        try:
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
        except (asyncio.CancelledError, Exception) as e:
            raise RuntimeError(f"notify error: {e}") from e

"""

with open("core/executor.py", "w") as f:
    f.write(code)
