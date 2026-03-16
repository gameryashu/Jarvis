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

async def run_voice(settings: Settings):
    """
    Hands-free wake-word voice mode.
    Continuously listens via pyaudio, transcribes with Whisper,
    checks for wake word, and runs the command pipeline.
    """
    global JARVIS_INTERRUPT

    try:
        import whisper as whisper_lib
        import pyaudio
        import numpy as np
    except ImportError as e:
        print(f"❌ Voice mode requires: whisper, pyaudio, numpy\n   pip install openai-whisper pyaudio numpy\n   Error: {e}")
        return

    print("\n🤖 JARVIS Voice Mode starting...")
    print("🔊 Loading Whisper model 'base'...")
    whisper_model = whisper_lib.load_model("base")
    print("✅ Whisper loaded.")

    memory = MemoryManager(settings)
    planner = LLMPlanner(settings, memory)
    executor = ActionExecutor(settings)
    feedback = FeedbackLoop(settings, executor)

    SAMPLE_RATE = settings.voice_sample_rate  # 16000
    SILENCE_THRESHOLD = settings.voice_silence_threshold  # 0.01
    SILENCE_DURATION = settings.voice_silence_duration    # 1.5
    CHUNK = 1024

    pa = pyaudio.PyAudio()
    stream = pa.open(
        format=pyaudio.paFloat32,
        channels=1,
        rate=SAMPLE_RATE,
        input=True,
        frames_per_buffer=CHUNK,
    )

    print(f"\n🎤 Listening for 'Hey Jarvis'... (Ctrl+C to stop)\n")
    wake_words = [w.lower() for w in settings.wake_words]

    loop = asyncio.get_event_loop()
    recording = False
    frames: list = []
    silence_start: Optional[float] = None

    import time

    try:
        while True:
            data = await loop.run_in_executor(None, stream.read, CHUNK, False)
            samples = np.frombuffer(data, dtype=np.float32)
            energy = float(np.sqrt(np.mean(samples ** 2)))
            is_speech = energy > SILENCE_THRESHOLD

            if is_speech:
                if not recording:
                    recording = True
                    frames = []
                frames.append(data)
                silence_start = None

            elif recording:
                frames.append(data)
                if silence_start is None:
                    silence_start = time.time()
                elif time.time() - silence_start >= SILENCE_DURATION:
                    recording = False
                    silence_start = None

                    # Transcribe
                    audio_bytes = b"".join(frames)
                    frames = []

                    try:
                        audio_np = np.frombuffer(audio_bytes, dtype=np.float32)

                        def _transcribe():
                            result = whisper_model.transcribe(
                                audio_np,
                                language=settings.stt_language,
                                fp16=False,
                            )
                            return result["text"].strip()

                        transcript = await loop.run_in_executor(None, _transcribe)
                        if not transcript:
                            continue

                        print(f"\n🎤 Heard: {transcript}")
                        transcript_lower = transcript.lower()

                        # Wake word check
                        found_wake = any(w in transcript_lower for w in wake_words)
                        if not found_wake:
                            continue

                        # Strip wake word
                        command = transcript
                        for w in wake_words:
                            idx = transcript_lower.find(w)
                            if idx != -1:
                                command = transcript[idx + len(w):].strip().lstrip(",").strip()
                                break

                        if not command:
                            await executor.speak("Yes, I'm listening.")
                            continue

                        print(f"📝 Command: {command}")
                        JARVIS_INTERRUPT = False

                        summary = await _autonomous_loop(command, planner, executor, feedback, memory)
                        print(f"\n✅ {summary}")
                        await executor.speak(summary)

                    except Exception as e:
                        logger.error("Voice pipeline error: %s", e, exc_info=True)
                        print(f"❌ Error: {e}")

            await asyncio.sleep(0)  # Yield control

    except KeyboardInterrupt:
        print("\n👋 Voice mode stopped.")
    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()
        await executor.close()
        print("👋 JARVIS offline.")


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

    if args.voice:
        asyncio.run(run_voice(settings))
    elif args.interactive:
        asyncio.run(run_interactive(settings, headless=args.headless))
    elif args.text:
        asyncio.run(run_text_command(args.text, settings))
    else:
        asyncio.run(run_jarvis(settings, headless=args.headless))


if __name__ == "__main__":
    main()
