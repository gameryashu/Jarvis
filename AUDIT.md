# JARVIS Codebase Audit & Implementation Plan

## 1. Bugs Found & Missing Features

### Bugs
1. **BUG 1: YouTube video never plays (Shadow DOM)**
   - **File/Line:** `core/executor.py` ~L358
   - **Status:** Current logic attempts to use JS to get YouTube video links but may fail. The system fallback to system browser is present but needs to be exact to the required specifications (wait for networkidle, wait 3s, use querySelectorAll on `ytd-video-renderer`, etc).
2. **BUG 2: Playwright mid-session death health check**
   - **File/Line:** `core/executor.py` `_ensure_playwright` ~L113
   - **Status:** Code already uses `evaluate("1+1")`. Wait, there is no `force=True` on `close()`. I will ensure we don't call it forcefully during normal operation.
3. **BUG 3: Voice doesn't speak responses after completing tasks**
   - **File/Line:** `main.py` ~L313 inside `run_voice`
   - **Status:** Voice speaking exists but the Voice Mode completely needs to be rewritten as per Phase 5 to directly load PyAudio and Whisper within the loop, and use `edge-tts`.
4. **BUG 4: Whisper hallucinations polluting voice input**
   - **File/Line:** `core/voice.py`
   - **Status:** Energy threshold is currently driven by Settings (0.01 or 0.05). Hallucination filter needs to be explicitly added to `voice.py` or the rewritten `run_voice` in `main.py`.
5. **BUG 5: Browser search uses Playwright on Google**
   - **File/Line:** `core/executor.py` ~L324
   - **Status:** `_tool_browser_search` currently searches via system browser. Needs to be renamed/adapted to `_tool_web_search` and restricted to NOT use Playwright for bot evasion.
6. **BUG 6: `ValueError: I/O operation on closed pipe` on exit**
   - **File/Line:** `main.py` `main()` ~L399
   - **Status:** The `asyncio.run()` needs to be wrapped in a `try...except KeyboardInterrupt:` block.
7. **BUG 7: Interrupt mechanism missing**
   - **File/Line:** `main.py`
   - **Status:** Although partially present, the interrupt mechanism logic needs to be completely verified inside `_autonomous_loop` and `run_interactive`.

### Missing Features (Tony Stark Features)
1. **FEATURE 1:** Proactive screen awareness. Missing `_tool_analyze_screen` in `executor.py`.
2. **FEATURE 2:** Smart memory. Semantic search functionality needs to be added to `memory.py` using `sentence-transformers` (or simply matching logic), injected via `llm.py`.
3. **FEATURE 3:** Multi-step workflows. Missing workflow executor in `main.py` logic / `heartbeat.py` integration for scheduled cron-like tasks.
4. **FEATURE 4:** Self-healing / Jarvis Status check command. Missing logic to intercept "jarvis status" and report via Playwright/Groq checks.
5. **FEATURE 5:** Code execution. Missing `_tool_run_code` in `executor.py`.
6. **FEATURE 6:** Clipboard intelligence. Needs explicit instructions for the AI to rewrite clipboards.
7. **FEATURE 7:** Window management. Missing `_tool_focus_window` in `executor.py`. Requires `pygetwindow`.
8. **FEATURE 8:** System monitoring. Missing `_tool_system_info` in `executor.py`. Requires `psutil`.

### Phase 4 & 5
- `core/llm.py` needs a complete `SYSTEM_PROMPT` swap.
- `main.py`'s `run_voice` requires a full replacement to handle streaming directly with PyAudio and Whisper arrays.

## 2. Implementation & Fix Plan

1. **Phase 2 (Bugs):**
   - Update `executor.py`: Fix YouTube querying, Playwright health checks, add `_tool_web_search`.
   - Update `main.py`: Fix `try/except KeyboardInterrupt` in `main()`.
   - Update `voice.py`: Although `run_voice` in `main.py` will handle most of the hallucination logic as per Phase 5, I will ensure `run_voice` has the exact `SILENCE_THRESHOLD = 0.04` and `HALLUCINATIONS` set.
2. **Phase 3 (Features):**
   - Append `_tool_analyze_screen`, `_tool_run_code`, `_tool_focus_window`, `_tool_system_info` to `executor.py`.
   - `pip install pygetwindow psutil` (since they are new dependencies).
   - Implement `jarvis status` check inside the LLM prompt or as a specialized handler in `executor.py`.
   - Extend `memory.py` with `search()` checking `sessions.jsonl`.
3. **Phase 4 & 5:**
   - Overwrite `SYSTEM_PROMPT` in `llm.py`.
   - Replace `run_voice` in `main.py`.
4. **Phase 6 & 7:**
   - Create the batch files.
   - Run tests sequentially. Fix any failures.

Review complete. Proceeding to execution steps.
