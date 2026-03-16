# 🤖 JARVIS — Personal AI Automation System

A Jarvis-style AI operator for your computer. Voice in, actions out.
Full computer control via LLM-powered planning, with persistent memory and a web dashboard.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  LAYER 1 — Voice Input                                  │
│  core/voice.py  →  Continuous mic capture + VAD         │
├─────────────────────────────────────────────────────────┤
│  LAYER 2 — Speech-to-Text                               │
│  core/stt.py    →  Whisper (local) / Google / Deepgram  │
├─────────────────────────────────────────────────────────┤
│  LAYER 3 — LLM Reasoning & Planning                     │
│  core/llm.py    →  Claude / GPT-4 / Ollama              │
│                     Converts text → structured Plan      │
├─────────────────────────────────────────────────────────┤
│  LAYER 4 — Action Execution                             │
│  core/executor.py → Mouse, Keyboard, Terminal,          │
│                      Browser, Files, TTS, Clipboard      │
├─────────────────────────────────────────────────────────┤
│  LAYER 5 — Feedback Loop                                │
│  core/feedback.py → Screenshot, OCR, Verification       │
└─────────────────────────────────────────────────────────┘
       ↕ Persistent context across sessions
  core/memory.py  →  Interaction log, projects, prefs
       ↕ Optional web UI
  ui/dashboard.py →  http://localhost:7860
```

---

## Quick Start

### 1. System Dependencies

**Linux (Ubuntu/Debian):**
```bash
sudo apt install portaudio19-dev tesseract-ocr ffmpeg python3-tk
```

**macOS:**
```bash
brew install portaudio tesseract ffmpeg
```

**Windows:**
- Install [Tesseract](https://github.com/UB-Mannheim/tesseract/wiki)
- Install [ffmpeg](https://ffmpeg.org/download.html)
- Install [PortAudio](http://www.portaudio.com/) (via pip usually works)

### 2. Python Environment

```bash
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. API Key

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
# OR edit config/settings.yaml
```

### 4. Run

```bash
# Full voice mode
python main.py

# Text mode (no microphone needed)
python main.py --text "Open Chrome and search for Python tutorials"

# Headless (no dashboard)
python main.py --headless
```

---

## Configuration

Edit `config/settings.yaml` to customize:

| Setting | Default | Description |
|---|---|---|
| `llm_provider` | `anthropic` | `anthropic` / `openai` / `ollama` |
| `llm_model` | `claude-opus-4-5` | Model to use |
| `stt_provider` | `whisper` | `whisper` / `google` / `deepgram` |
| `stt_model` | `base` | Whisper: `tiny`/`base`/`small`/`medium`/`large` |
| `tts_provider` | `pyttsx3` | `pyttsx3` / `elevenlabs` / `gtts` |
| `require_wake_word` | `true` | Require "Hey Jarvis" prefix |
| `confirm_destructive` | `true` | Prompt before deleting files |

---

## Example Commands

| You say | What JARVIS does |
|---|---|
| "Hey Jarvis, open Chrome and go to GitHub" | Launches Chrome, navigates to github.com |
| "Create a Python script that prints hello world and save it to my desktop" | Writes file, saves it |
| "Search for RWTH Aachen CS program requirements" | Opens browser, searches Google |
| "Take a screenshot and tell me what's on screen" | Screenshots, OCRs, summarizes |
| "Run the Python file on my desktop called test.py" | Executes via terminal |
| "Find all PDF files in my Downloads folder" | Runs `find` command, lists results |
| "Open VS Code and create a new file called main.py" | Opens app, creates file |
| "What did I ask you to do earlier today?" | Searches memory log |

---

## Using a Local LLM (Offline Mode)

Install [Ollama](https://ollama.ai), then:
```bash
ollama pull llama3.2
```
Edit `config/settings.yaml`:
```yaml
llm_provider: ollama
llm_model: llama3.2
llm_base_url: http://localhost:11434
```

---

## Adding Custom Tools

Create a new method in `core/executor.py`:
```python
async def _tool_my_custom_tool(self, params: dict):
    # params comes from LLM's JSON plan
    value = params.get("my_param")
    # do something
    return "result"
```

Then add it to the system prompt in `core/llm.py` under the tools list.

---

## Memory

All interactions are stored in `~/.jarvis/memory/`:
- `sessions.jsonl` — full interaction log
- `projects.json` — named projects with notes
- `preferences.json` — learned preferences

JARVIS automatically injects recent history as context into each LLM call.

---

## Web Dashboard

Starts automatically at `http://localhost:7860`.
- Live activity log
- Manual text command input
- Session statistics
- Command history (click to replay)

---

## Project Structure

```
jarvis/
├── main.py              # Entry point
├── requirements.txt
├── config/
│   ├── settings.py      # Settings dataclass
│   └── settings.yaml    # User config
├── core/
│   ├── voice.py         # Layer 1: Mic capture + VAD
│   ├── stt.py           # Layer 2: Speech-to-text
│   ├── llm.py           # Layer 3: LLM planning
│   ├── executor.py      # Layer 4: Action execution
│   ├── feedback.py      # Layer 5: Feedback loop
│   └── memory.py        # Persistent memory
└── ui/
    └── dashboard.py     # Web UI
```

---

## Roadmap / Extensions

- [ ] Browser automation with Playwright (beyond just opening URLs)
- [ ] Calendar / email integration
- [ ] Screen region targeting via vision model
- [ ] Multi-step task queue with pause/resume
- [ ] Plugin system (drop `.py` file in `plugins/` folder)
- [ ] ElevenLabs voice cloning for personalized TTS
- [ ] German language support (for your learning goals 🇩🇪)
- [ ] GitHub integration (auto-commit, PR review)

---

## Troubleshooting

**"pyaudio not found"** → Install portaudio first, then `pip install pyaudio`

**"whisper not found"** → `pip install openai-whisper` (also needs ffmpeg)

**"pytesseract not found"** → Install system tesseract + `pip install pytesseract`

**No sound from TTS** → Try `tts_provider: gtts` in settings.yaml

**LLM returns invalid JSON** → Normal occasionally; JARVIS auto-recovers with a fallback plan

---

*Built for personal automation. Use responsibly — with `confirm_destructive: true`, JARVIS will always ask before deleting files or making irreversible changes.*
