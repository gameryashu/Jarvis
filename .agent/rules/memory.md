# Memory & Context Skill  
JARVIS has persistent memory at ~/.jarvis/memory/
- sessions.jsonl: all past interactions
- action_log.txt: every tool execution
- projects.json: named projects

When user says "remember this" or "save this":
Use memory.save_interaction or write to projects.json

When user asks "what did I do earlier" or "last time":
Read from sessions.jsonl and summarize
