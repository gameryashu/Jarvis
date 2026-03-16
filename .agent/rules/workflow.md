# Antigravity Agent Workflow Rules

## Planning Phase
ALWAYS use Gemini 2.5 Pro in Planning mode for:
- Breaking down new features into specs
- Architecture decisions
- Multi-file refactoring plans
- Do NOT write code in this phase

## Execution Phase  
ALWAYS use Claude Opus 4.6 in Fast mode for:
- Writing actual code from the spec
- Fixing bugs and errors
- Editing specific files
- Running terminal commands

## Python Interpreter
ALWAYS use: Y:\jarvis\venv\Scripts\python.exe
NEVER use system Python

## Project Structure
Root: Y:\jarvis\jarvis
Venv: Y:\jarvis\venv
Run JARVIS: Y:\jarvis\venv\Scripts\python.exe Y:\jarvis\jarvis\main.py --text "command"
Short alias: jrun "command"

## Testing
After every code change, test with:
jrun "open calculator"
jrun "open youtube"
