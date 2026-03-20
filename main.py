#!/usr/bin/env python3
"""
JARVIS - Personal AI Automation System
Entry point: starts voice listener, LLM planner, and action executor.

Modes:
  --interactive   Interactive CLI prompt loop
  --text CMD      One-shot text command
  --voice         Hands-free wake-word voice mode
"""

import asyncio
import argparse
import logging
import sys
import threading
from pathlib import Path
from typing import Optional

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent))

from core.llm import LLMPlanner, Plan
from core.executor import ActionExecutor
from core.feedback import FeedbackLoop
from core.memory import MemoryManager
from config.settings import Settings
from core.heartbeat import heartbeat_loop
from core.telegram_bot import TelegramBot

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── Global interrupt flag ─────────────────────────────────────────────────────
JARVIS_INTERRUPT = False


def _watch_for_interrupt():
    """Background thread: set JARVIS_INTERRUPT when user presses 'q' + Enter."""
    global JARVIS_INTERRUPT
    while True:
        try:
            key = input()
            if key.strip().lower() == "q":
                JARVIS_INTERRUPT = True
                print("\n⚡ Interrupt requested — finishing current step...")
        except (EOFError, OSError):
            break


def _start_interrupt_watcher():
    """Start the interrupt-watcher thread as a daemon."""
    t = threading.Thread(target=_watch_for_interrupt, daemon=True)
    t.start()


# ── Autonomous execution loop ─────────────────────────────────────────────────

async def _autonomous_loop(
    command: str,
    planner: LLMPlanner,
    executor: ActionExecutor,
    feedback: FeedbackLoop,
    memory: MemoryManager,
    max_iterations: int = 5,
) -> str:
    """
    Plan → Execute → Verify → (maybe Recover) loop.
    - Max 5 iterations to avoid burning tokens on infinite retries.
    - Tracks failed tool+description combos to skip repeated failures.
    - Checks JARVIS_INTERRUPT before each step.
    Returns a summary string.
    """
    global JARVIS_INTERRUPT

    try:
        if command.strip().lower() in ("status", "jarvis status", "are you working"):
            import httpx, os
            base = os.getenv("OPENAI_BASE_URL", "https://api.groq.com/openai/v1/")
            key = os.getenv("OPENAI_API_KEY", "")
            try:
                with httpx.Client() as client:
                    resp = client.get(f"{base}models", headers={"Authorization": f"Bearer {key}"}, timeout=5)
                    groq_ok = resp.status_code == 200
            except Exception:
                groq_ok = False
            
            p_ok = executor._persistent_page is not None
            mem_size = 0
            try:
                if memory._sessions_path.exists():
                    mem_size = memory._sessions_path.stat().st_size
            except Exception: pass
            
            status_text = "All systems operational. " if groq_ok else "Warning: Groq API unreachable. "
            if recent_acts := memory.get_recent_context(5):
                status_text += f"Last action was: {recent_acts[-1].split('→')[-1].strip()}."
            else:
                status_text += "No recent actions in memory."
            
            print(status_text)
            return status_text
    except Exception as e:
        logger.error("Status check error: %s", e)

    plan: Optional[Plan] = None
    results: list = []
    failed_tools: set = set()
    iteration = 0

    try:
        plan = await planner.plan(command)
        print(f"📋 Plan:\n{plan.summary()}")
    except Exception as e:
        logger.error("Planning failed: %s", e)
        print(f"❌ Planning error: {e}")
        return "Command failed — could not generate a plan."

    for step in plan.steps:
        if iteration >= max_iterations:
            print(f"⚠️  Max iterations ({max_iterations}) reached — stopping.")
            break

        # Interrupt check
        if JARVIS_INTERRUPT:
            print("⚡ Interrupted by user.")
            break

        # Skip known-failing tool+description combos
        step_key = f"{step.tool}:{step.description}"
        if step_key in failed_tools:
            print(f"   ⏭️  Skipping previously failed step: {step.description}")
            continue

        print(f"  ⚙️  {step.description}")
        iteration += 1

        try:  
            result = await executor.execute(step)
            results.append(result)

            verified = await feedback.verify(step, result)

            if not verified.success:
                print(f"  ⚠️  Step failed: {verified.reason}")
                failed_tools.add(step_key)

                # Attempt recovery (only once per failure)
                try:
                    recovery = await planner.recover(step, verified)
                    if recovery:
                        rec_key = f"{recovery.tool}:{recovery.description}"
                        if rec_key not in failed_tools:
                            print(f"  🔄 Attempting recovery: {recovery.description}")
                            rec_result = await executor.execute(recovery)
                            results.append(rec_result)
                            if not rec_result.success:
                                failed_tools.add(rec_key)
                except Exception as rec_err:
                    logger.warning("Recovery planning failed: %s", rec_err)

        except Exception as step_err:
            logger.error("Step execution error: %s", step_err)
            print(f"  ❌ Step error: {step_err}")
            failed_tools.add(step_key)

    try:
        summary = await planner.summarize(plan, results)
    except Exception as e:
        logger.warning("Summarize failed: %s", e)
        summary = f"Completed with {len(results)} step(s)."

    memory.save_interaction(command, plan, results)
    return summary


# ── Interactive mode ──────────────────────────────────────────────────────────

async def run_interactive(settings: Settings, headless: bool = False):
    """Run interactive CLI prompt loop."""
    global JARVIS_INTERRUPT

    print("\nJARVIS online. Type commands or 'exit' to quit.")
    print("💡 Press 'q' + Enter at any time to interrupt a running task.\n")

    _start_interrupt_watcher()

    memory = MemoryManager(settings)
    planner = LLMPlanner(settings, memory)
    executor = ActionExecutor(settings)
    feedback = FeedbackLoop(settings, executor)

    telegram_bot = TelegramBot(planner, executor, feedback, memory)
    asyncio.create_task(telegram_bot.start())
    asyncio.create_task(heartbeat_loop(planner, executor, feedback))

    try:
        while True:
            try:
                command = input("\n💬 ")
            except EOFError:
                break

            cmd_lower = command.strip().lower()
            if cmd_lower in ("exit", "quit"):
                break
            if not cmd_lower:
                continue

            JARVIS_INTERRUPT = False  # Reset flag before each command
            print(f"📝 Command: {command}")

            summary = await _autonomous_loop(command, planner, executor, feedback, memory)
            print(f"\n✅ {summary}")
            await executor.speak(summary)

    finally:
        await executor.close()
        print("👋 JARVIS offline.")


# ── Voice mode ────────────────────────────────────────────────────────────────

async def run_voice(settings):
    print("\n🤖 JARVIS initializing...\n")
    
    # Initialize all components ONCE
    memory = MemoryManager(settings)
    planner = LLMPlanner(settings, memory)
    executor = ActionExecutor(settings)
    feedback = FeedbackLoop(settings, executor)
    memory.log_session_start()
    
    # Load Whisper
    import whisper
    print("🔊 Loading Whisper model...")
    model = whisper.load_model(settings.stt_model)
    print("✅ Whisper loaded.\n")
    
    # Speak startup
    await executor.speak("JARVIS online. All systems operational.")
    print("🎤 Listening for 'Hey Jarvis'... (Press Ctrl+C to stop)\n")
    
    import pyaudio, numpy as np
    pa = pyaudio.PyAudio()
    CHUNK, RATE = 1024, 16000
    SILENCE_THRESHOLD = 0.04
    SILENCE_DURATION = 1.5
    
    HALLUCINATIONS = {"thanks for watching","thank you","subscribe","bye",
                      "like and share","see you","hello","you","the","a","i"}
    ACTION_WORDS = {"open","play","search","find","create","write","tell","what",
                    "show","go","start","stop","close","type","run","make","get",
                    "time","date","jarvis","how","where","when","is","are","can",
                    "take","screenshot","folder","file","download","launch","music"}
    
    stream = pa.open(format=pyaudio.paFloat32, channels=1, rate=RATE,
                     input=True, frames_per_buffer=CHUNK)
    
    recording = False
    frames = []
    silence_start = None
    loop = asyncio.get_event_loop()
    
    try:
        while True:
            data = await loop.run_in_executor(None, stream.read, CHUNK, False)
            samples = np.frombuffer(data, dtype=np.float32)
            energy = float(np.sqrt(np.mean(samples**2)))
            
            if energy > SILENCE_THRESHOLD:
                if not recording:
                    recording = True
                    frames = []
                frames.append(data)
                silence_start = None
            elif recording:
                frames.append(data)
                if silence_start is None:
                    import time
                    silence_start = time.time()
                elif time.time() - silence_start >= SILENCE_DURATION:
                    recording = False
                    audio_np = np.frombuffer(b"".join(frames), dtype=np.float32)
                    
                    def _transcribe():
                        result = model.transcribe(audio_np, language="en", fp16=False)
                        return result["text"].strip().lower()
                    
                    transcript = await loop.run_in_executor(None, _transcribe)
                    words = transcript.split()
                    
                    # Filter hallucinations
                    if len(words) < 4: continue
                    if transcript in HALLUCINATIONS: continue
                    if not any(w in ACTION_WORDS for w in words): continue
                    
                    print(f"🎤 Heard: {transcript}")
                    
                    # Check wake word
                    has_wake = "jarvis" in transcript
                    if not has_wake: continue
                    
                    # Strip wake word
                    for wake in ["hey jarvis", "jarvis"]:
                        if wake in transcript:
                            command = transcript.split(wake, 1)[-1].strip().lstrip(",").strip()
                            break
                    
                    if not command or len(command) < 2: continue
                    
                    print(f"📝 Command: {command}")
                    
                    try:
                        plan = await planner.plan(command)
                        results = []
                        for step in plan.steps:
                            print(f"  ⚙️  {step.description}")
                            result = await executor.execute(step)
                            results.append(result)
                            verified = await feedback.verify(step, result)
                            if not verified.success:
                                recovery = await planner.recover(step, verified)
                                if recovery:
                                    await executor.execute(recovery)
                        
                        summary = await planner.summarize(plan, results)
                        print(f"✅ {summary}")
                        await executor.speak(summary)
                        memory.save_interaction(command, plan, results)
                    
                    except Exception as e:
                        print(f"❌ Error: {e}")
                        await executor.speak("I encountered an error. Please try again.")
                    
                    frames = []
                    silence_start = None
    
    except KeyboardInterrupt:
        pass
    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()
        memory.log_session_end()
        print("\n👋 JARVIS offline.")


# ── One-shot text mode ────────────────────────────────────────────────────────

async def run_text_command(command: str, settings: Settings):
    """Run a single command in text mode (no voice)."""
    memory = MemoryManager(settings)
    planner = LLMPlanner(settings, memory)
    executor = ActionExecutor(settings)
    feedback = FeedbackLoop(settings, executor)

    print(f"📝 Command: {command}")

    summary = await _autonomous_loop(command, planner, executor, feedback, memory)
    print(f"\n✅ {summary}")

    try:
        await executor.close()
    except Exception as e:
        logger.warning("Executor close error: %s", e)


# ── Legacy full voice loop (original mode) ────────────────────────────────────

async def run_jarvis(settings: Settings, headless: bool = False):
    """Legacy async loop for Jarvis using VoiceListener + SpeechToText."""
    from core.voice import VoiceListener
    from core.stt import SpeechToText

    print("\n🤖 JARVIS initializing...\n")

    memory = MemoryManager(settings)
    stt = SpeechToText(settings)
    planner = LLMPlanner(settings, memory)
    executor = ActionExecutor(settings)
    feedback = FeedbackLoop(settings, executor)

    print("✅ All systems online. Say 'Hey Jarvis' or press Ctrl+C to exit.\n")
    memory.log_session_start()

    async with VoiceListener(settings) as voice:
        async for audio_chunk in voice.stream():
            try:
                transcript = await stt.transcribe(audio_chunk)
                if not transcript:
                    continue

                print(f"\n🎤 Heard: {transcript}")

                if settings.require_wake_word and not stt.has_wake_word(transcript):
                    continue

                cleaned = stt.strip_wake_word(transcript)

                summary = await _autonomous_loop(cleaned, planner, executor, feedback, memory)
                print(f"\n✅ Done: {summary}")
                await executor.speak(summary)

            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error("Main loop error: %s", e, exc_info=True)
                print(f"❌ Error: {e}")
                await executor.speak(f"I encountered an error: {str(e)}")

    memory.log_session_end()
    print("\n👋 JARVIS offline.")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="JARVIS AI Automation System")
    parser.add_argument("--headless", action="store_true", help="Run without dashboard UI")
    parser.add_argument("--config", default="config/settings.yaml", help="Config file path")
    parser.add_argument("--text", help="Run a single text command instead of voice")
    parser.add_argument("--interactive", action="store_true", help="Run in interactive CLI mode")
    parser.add_argument("--voice", action="store_true", help="Run in hands-free wake-word voice mode")
    args = parser.parse_args()

    settings = Settings.load(args.config)

    try:
        if args.voice:
            asyncio.run(run_voice(settings))
        elif args.interactive:
            asyncio.run(run_interactive(settings, headless=args.headless))
        elif args.text:
            asyncio.run(run_text_command(args.text, settings))
        else:
            asyncio.run(run_jarvis(settings, headless=args.headless))
    except (KeyboardInterrupt, ValueError):
        pass


if __name__ == "__main__":
    main()
