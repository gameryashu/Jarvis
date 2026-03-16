"""
patch.py — Run this once to fix executor.py on Windows.
Run from Y:\\jarvis\\jarvis with: python patch.py
"""

import pathlib
import sys

f = pathlib.Path("core/executor.py")

if not f.exists():
    print("ERROR: core/executor.py not found. Make sure you're in Y:\\jarvis\\jarvis")
    sys.exit(1)

code = f.read_text(encoding="utf-8", errors="replace")

# ── Fix 1: Replace open_app method entirely ──────────────────────────────────
OLD_OPEN_APP = '''    async def _tool_open_app(self, params: dict):
        app = params.get("app", "").lower().strip()
        loop = asyncio.get_event_loop()

        # Windows app name aliases — maps common names to actual commands
        WINDOWS_APPS = {'''

NEW_OPEN_APP = '''    async def _tool_open_app(self, params: dict):
        import shutil
        raw = params.get("app", "")
        app = raw.lower().strip()
        loop = asyncio.get_event_loop()

        # Windows app aliases
        WINDOWS_APPS = {
            "calculator": "calc",
            "calc": "calc",
            "notepad": "notepad",
            "paint": "mspaint",
            "wordpad": "wordpad",
            "file explorer": "explorer",
            "explorer": "explorer",
            "task manager": "taskmgr",
            "control panel": "control",
            "cmd": "cmd",
            "command prompt": "cmd",
            "powershell": "powershell",
            "chrome": "chrome",
            "google chrome": "chrome",
            "firefox": "firefox",
            "edge": "msedge",
            "microsoft edge": "msedge",
            "vs code": "code",
            "vscode": "code",
            "visual studio code": "code",
            "antigravity": "antigravity",
            "spotify": "spotify",
            "discord": "discord",
            "steam": "steam",
            "vlc": "vlc",
            "winrar": "winrar",
        }'''

# ── Fix 2: Replace the broken _open() function inside open_app ───────────────
OLD_OPEN_FUNC = '''        def _open():
            if os.name == "nt":
                # Look up alias first
                cmd = WINDOWS_APPS.get(app, app)
                try:
                    subprocess.Popen(cmd, shell=True)
                except Exception:
                    os.startfile(cmd)
            elif hasattr(os, "uname") and os.uname().sysname == "Darwin":
                subprocess.Popen(["open", "-a", app])
            else:
                subprocess.Popen([app])

        await loop.run_in_executor(None, _open)'''

NEW_OPEN_FUNC = '''        def _open():
            if os.name == "nt":
                cmd = WINDOWS_APPS.get(app, raw)
                try:
                    subprocess.Popen(f'start "" "{cmd}"', shell=True)
                except Exception:
                    try:
                        subprocess.Popen(cmd, shell=True)
                    except Exception:
                        os.startfile(cmd)
            elif hasattr(os, "uname") and os.uname().sysname == "Darwin":
                subprocess.Popen(["open", "-a", raw])
            else:
                subprocess.Popen([raw])

        await loop.run_in_executor(None, _open)'''

# ── Fix 3: Fix terminal tool to use Windows-compatible desktop path ───────────
OLD_MKDIR = "mkdir ~/Desktop/"
NEW_MKDIR = r"mkdir %USERPROFILE%\Desktop\ "

# ── Fix 4: Fix file_write desktop path ───────────────────────────────────────
OLD_FILE_WRITE = '''    async def _tool_file_write(self, params: dict):
        path = Path(params.get("path", "")).expanduser()'''

NEW_FILE_WRITE = '''    async def _tool_file_write(self, params: dict):
        import os as _os
        raw_path = params.get("path", "")
        desktop = str(pathlib.Path.home() / "Desktop")
        raw_path = raw_path.replace("~/Desktop", desktop)
        raw_path = raw_path.replace("/Users/username/Desktop", desktop)
        raw_path = raw_path.replace("~\\\\Desktop", desktop)
        path = Path(raw_path).expanduser()'''

# Apply all fixes
fixes = [
    (OLD_OPEN_APP, NEW_OPEN_APP, "open_app aliases"),
    (OLD_OPEN_FUNC, NEW_OPEN_FUNC, "open_app _open() function"),
    (OLD_MKDIR, NEW_MKDIR, "mkdir desktop path"),
    (OLD_FILE_WRITE, NEW_FILE_WRITE, "file_write desktop path"),
]

applied = 0
for old, new, name in fixes:
    if old in code:
        code = code.replace(old, new)
        print(f"  ✅ Fixed: {name}")
        applied += 1
    else:
        print(f"  ⏭️  Skipped (already fixed or not found): {name}")

# Also add create_folder tool if missing
if "_tool_create_folder" not in code:
    CREATE_FOLDER = '''
    async def _tool_create_folder(self, params: dict):
        """Create a directory on the filesystem."""
        import os as _os
        raw_path = params.get("path", "")
        desktop = str(pathlib.Path.home() / "Desktop")
        raw_path = raw_path.replace("~/Desktop", desktop)
        raw_path = raw_path.replace("/Users/username/Desktop", desktop)
        path = Path(raw_path).expanduser()
        loop = asyncio.get_event_loop()

        def _mkdir():
            path.mkdir(parents=True, exist_ok=True)
            print(f"   📁 Folder created: {path}")

        await loop.run_in_executor(None, _mkdir)
'''
    # Insert before file_delete
    code = code.replace(
        "    async def _tool_file_delete",
        CREATE_FOLDER + "    async def _tool_file_delete"
    )
    print("  ✅ Added: create_folder tool")
    applied += 1

# Write back
f.write_text(code, encoding="utf-8")
print(f"\n✅ Patch complete. {applied} fixes applied.")
print("\nNow test with:")
print('  python main.py --text "open calculator"')
print('  python main.py --text "create a folder called JarvisTest on my desktop"')
print('  python main.py --text "take a screenshot"')
