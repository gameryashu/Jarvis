---
name: jarvis-operator
description: Use when controlling the computer, running JARVIS commands, opening apps, browsing the web, creating files/folders, or performing any computer automation task. Activates for phrases like 'open', 'go to', 'search', 'create folder', 'run', 'execute'.
---

# JARVIS Computer Operator Skill

## How to run JARVIS
Always use interactive mode to keep browser alive:
Y:\jarvis\venv\Scripts\python.exe Y:\jarvis\jarvis\main.py --interactive

## For single commands:
jrun "your command here"

## Python interpreter:
Y:\jarvis\venv\Scripts\python.exe

## Project root: Y:\jarvis\jarvis

## Key rules:
- For YouTube search: use browser_search tool, NEVER browser_type
- Desktop path: C:\Users\yashu\Desktop
- Always test after changes: jrun "open calculator"
