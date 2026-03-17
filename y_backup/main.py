#!/usr/bin/env python3
"""
JARVIS - Personal AI Automation System
Entry point: starts voice listener, LLM planner, and action executor.
Features an autonomous completion loop that keeps running until the goal is confirmed complete.
"""

import asyncio
import argparse
import sys
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent))

from core.voice import VoiceListener
from core.llm import LLMPlanner
from core.executor import ActionExecutor, _log_action
from core.feedback import FeedbackLoop
from core.memory import MemoryManager
from ui.dashboard import Dashboard
from config.settings import Settings
from core.heartbeat import heartbeat_loop
from core.telegram_bot import TelegramBot


async def _autonomous_loop(
    goal: str,
    planner: LLMPlanner,
    executor: ActionExecutor,
    feedback: FeedbackLoop,
    memory: MemoryManager,
    max_iterations: int = 15,
) -> str:
    """
    Autonomous completion loop.
    Plans, executes, verifies via vision, and re-plans until the goal is confirmed
    complete by the LLM — or the max iteration safety cutoff is reached.

    Returns a final summary string.
    """
    history: list[str] = []
    last_vision_feedback = ""
    iteration = 0

    while iteration < max_iterations:
        iteration += 1
        print(f"\n🔄 Autonomous loop — iteration {iteration}/{max_iterations}")

        # ── 1. Plan (include execution history for re-planning) ───────────
        try:
            context_command = goal
            if history:
                history_str = "\n".join(f"  {i+1}. {h}" for i, h in enumerate(history))
                context_command = (
                    f"{goal}\n\n"
                    f"Steps already completed:\n{history_str}\n\n"
                    f"Latest screen analysis: {last_vision_feedback}\n\n"
                    f"Plan ONLY the remaining steps needed. "
                    f"If the goal is already done, return an empty steps array."
                )
            plan = await planner.plan(context_command)
        except Exception as e:
            print(f"  ❌ LLM planning failed: {type(e).__name__}: {e}")
            _log_action("planner", {"command": goal}, False, str(e))
            await executor.speak("Sorry, I couldn't plan the next steps.")
            break

        # If the planner returned zero real steps, the goal might be done
        if not plan.steps or (
            len(plan.steps) == 1 and plan.steps[0].tool == "speak"
            and "no action" in plan.steps[0].description.lower()
        ):
            print("  ✅ Planner returned no further action steps.")
            break

        print(f"📋 Plan:\n{plan.summary()}")

        # ── 2. Execute each step ──────────────────────────────────────────
        results = []
        for i, step in enumerate(plan.steps, 1):
            print(f"  ⚙️  [{i}/{len(plan.steps)}] {step.description}")
            result = await executor.execute(step)
            results.append(result)

            status = "✅" if result.success else "❌"
            history.append(f"{status} [{step.tool}] {step.description}")

            if not result.success:
                print(f"  ❌ Step {i} failed: {result.error}")

            # ── 3. Feedback: verify with vision ───────────────────────────
            verified = await feedback.verify(step, result)
            if not verified.success:
                print(f"  ⚠️  Verification failed: {verified.reason}")
                history.append(f"⚠️ Verification: {verified.reason}")

                recovery = await planner.recover(step, verified)
                if recovery:
                    print(f"  🔧 Recovery: {recovery.description}")
                    rec_result = await executor.execute(recovery)
                    history.append(
                        f"🔧 Recovery [{recovery.tool}] {recovery.description} "
                        f"{'✅' if rec_result.success else '❌'}"
                    )
            else:
                last_vision_feedback = verified.reason

        # ── 4. Check if the goal is complete ──────────────────────────────
        try:
            complete = await planner.is_goal_complete(
                goal, history, last_vision_feedback
            )
            if complete:
                print("  🎯 Goal confirmed complete by LLM.")
                break
            else:
                print("  🔁 Goal not yet complete. Re-planning...")
        except Exception as e:
            print(f"  ⚠️  Goal-completion check failed: {e}. Assuming done.")
            break

    else:
        # max_iterations reached
        print(f"\n⚠️  Reached max iterations ({max_iterations}). Stopping autonomous loop.")
        await executor.speak(
            f"I've tried {max_iterations} iterations but couldn't fully complete the goal. "
            f"Please check the results."
        )

    # ── 5. Summarize ──────────────────────────────────────────────────────
    if plan is None:
        return "I couldn't create a plan for this goal."
    summary = await planner.summarize(plan, results)
    return summary


async def run_jarvis(settings: Settings, headless: bool = False):
    """Main async loop for Jarvis."""
    print("\n🤖 JARVIS initializing...\n")

    # Initialize components
    memory = MemoryManager(settings)
    planner = LLMPlanner(settings, memory)
    executor = ActionExecutor(settings)
    feedback = FeedbackLoop(settings, executor)

    if not headless:
        dashboard = Dashboard(settings)
        asyncio.create_task(dashboard.start())

    print("✅ All systems online. Say 'Hey Jarvis' or press Ctrl+C to exit.\n")
    memory.log_session_start()

    # Core event loop — voice.stream() now yields transcribed strings
    async with VoiceListener(settings) as voice:
        async for transcript in voice.stream():
            try:
                if not transcript:
                    continue

                print(f"\n🎤 Heard: {transcript}")

                # Wake word check (optional)
                if settings.require_wake_word and not voice.has_wake_word(transcript):
                    continue

                cleaned = voice.strip_wake_word(transcript)

                # Run autonomous completion loop
                summary = await _autonomous_loop(
                    goal=cleaned,
                    planner=planner,
                    executor=executor,
                    feedback=feedback,
                    memory=memory,
                    max_iterations=settings.autonomous_max_iterations,
                )

                print(f"\n✅ Done: {summary}")
                await executor.speak(summary)

                # Persist to memory
                memory.save_interaction(transcript, planner._last_plan, [])

            except KeyboardInterrupt:
                break
            except Exception as e:
                err_msg = f"{type(e).__name__}: {e}"
                print(f"❌ Unexpected error: {err_msg}")
                _log_action("main_loop", {}, False, err_msg)
                await executor.speak(f"Something went wrong: {str(e)[:80]}")

    # Cleanup Playwright if it was initialized
    await executor._cleanup_playwright()
    memory.log_session_end()
    print("\n👋 JARVIS offline.")


def main():
    parser = argparse.ArgumentParser(description="JARVIS AI Automation System")
    parser.add_argument("--headless", action="store_true", help="Run without dashboard UI")
    parser.add_argument("--server", action="store_true", help="Start the persistent JARVIS server")
    parser.add_argument("--interactive", action="store_true", help="Start the interactive CLI session")
    parser.add_argument("--config", default="config/settings.yaml", help="Config file path")
    parser.add_argument("--text", help="Run a single text command instead of voice")
    args = parser.parse_args()

    settings = Settings.load(args.config)

    if args.server:
        from core.server import start_server
        asyncio.run(start_server(settings, _autonomous_loop))
    elif args.interactive:
        asyncio.run(run_interactive(settings, headless=args.headless))
    elif args.text:
        # One-shot text mode for testing
        asyncio.run(run_text_command(args.text, settings))
    else:
        asyncio.run(run_jarvis(settings, headless=args.headless))


async def run_interactive(settings: Settings, headless: bool = False):
    """Run interactive mode loop."""
    print("\nJARVIS online. Type commands or 'exit' to quit.")
    
    # Initialize components ONLY ONCE
    memory = MemoryManager(settings)
    planner = LLMPlanner(settings, memory)
    executor = ActionExecutor(settings)
    feedback = FeedbackLoop(settings, executor)
    
    # Start Heartbeat and Telegram Bot
    telegram_bot = TelegramBot(planner, executor, feedback, memory)
    asyncio.create_task(telegram_bot.start())
    asyncio.create_task(heartbeat_loop(planner, executor, feedback))
    
    try:
        while True:
            # Wait for input loop
            try:
                command = await asyncio.get_event_loop().run_in_executor(None, input, "\n💬 ")
            except EOFError:
                break

            cmd_lower = command.strip().lower()
            if cmd_lower in ("exit", "quit"):
                break
                
            if not cmd_lower:
                continue

            print(f"📝 Command: {command}")
            summary = await _autonomous_loop(
                goal=command,
                planner=planner,
                executor=executor,
                feedback=feedback,
                memory=memory,
                max_iterations=settings.autonomous_max_iterations,
            )
            print(f"\n✅ {summary}")
            memory.save_interaction(command, getattr(planner, '_last_plan', None), [])
            
    finally:
        await executor._cleanup_playwright()
        print("👋 JARVIS offline.")


async def run_text_command(command: str, settings: Settings):
    """Run a single command in text mode using the autonomous completion loop."""
    memory = MemoryManager(settings)
    planner = LLMPlanner(settings, memory)
    executor = ActionExecutor(settings)
    feedback = FeedbackLoop(settings, executor)

    print(f"📝 Command: {command}")

    summary = await _autonomous_loop(
        goal=command,
        planner=planner,
        executor=executor,
        feedback=feedback,
        memory=memory,
        max_iterations=settings.autonomous_max_iterations,
    )

    print(f"\n✅ {summary}")
    memory.save_interaction(command, planner._last_plan if hasattr(planner, '_last_plan') else None, [])

    # Cleanup Playwright
    await executor._cleanup_playwright()


if __name__ == "__main__":
    main()
