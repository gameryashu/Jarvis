"""
core/heartbeat.py — Background task scheduler.
Runs every 60 seconds.
Checks ~/.jarvis/memory/tasks.json for pending tasks.
Executes any task whose scheduled_time has passed.
Logs completion back to tasks.json.
"""

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

async def heartbeat_loop(planner, executor, feedback):
    memory_dir = Path(os.path.expanduser("~/.jarvis/memory"))
    memory_dir.mkdir(parents=True, exist_ok=True)
    tasks_file = memory_dir / "tasks.json"
    
    while True:
        try:
            if tasks_file.exists():
                with open(tasks_file, "r", encoding="utf-8") as f:
                    try:
                        tasks = json.load(f)
                    except json.JSONDecodeError:
                        tasks = []
                
                changed = False
                now = datetime.now()
                
                for task in tasks:
                    if not task.get("done", False):
                        sched_str = task.get("scheduled_time")
                        if sched_str:
                            try:
                                sched_time = datetime.fromisoformat(sched_str)
                                if now >= sched_time:
                                    print(f"\n⏰ Executing scheduled task: {task['task']}")
                                    plan = await planner.plan(task["task"])
                                    results = []
                                    for step in plan.steps:
                                        print(f"  ⚙️  {step.description}")
                                        result = await executor.execute(step)
                                        results.append(result)
                                        verified = await feedback.verify(step, result)
                                        if not verified.success:
                                            print(f"  ⚠️  Step failed: {verified.reason}")
                                            recovery = await planner.recover(step, verified)
                                            if recovery:
                                                await executor.execute(recovery)
                                    task["done"] = True
                                    changed = True
                            except ValueError:
                                pass
                                
                if changed:
                    with open(tasks_file, "w", encoding="utf-8") as f:
                        json.dump(tasks, f, indent=2)
                        
        except Exception as e:
            print(f"Heartbeat error: {e}")
            
        await asyncio.sleep(60)
