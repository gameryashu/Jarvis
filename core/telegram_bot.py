"""
core/telegram_bot.py — Telegram bot interface for JARVIS.
Receives text messages and passes them to JARVIS planner.
Sends back result as message.
Sends screenshots as photo messages.
"""

import os
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

class TelegramBot:
    def __init__(self, planner, executor, feedback, memory):
        self.planner = planner
        self.executor = executor
        self.feedback = feedback
        self.memory = memory
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN")
        self.whitelist_id = os.environ.get("TELEGRAM_USER_ID")
        self.app = None
        
    async def start(self):
        if not self.token or not self.whitelist_id:
            print("⚠️  Telegram bot token or user ID not found. Telegram interface disabled.")
            return
            
        self.app = Application.builder().token(self.token).build()
        self.app.add_handler(CommandHandler("start", self._handle_start))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message))
        
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()
        print("📱 Telegram interface online.")
        
    async def _is_allowed(self, update: Update) -> bool:
        if str(update.effective_user.id) != self.whitelist_id:
            await update.message.reply_text("Unauthorized user.")
            return False
        return True
        
    async def _handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._is_allowed(update): return
        await update.message.reply_text("JARVIS is online.")
        
    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._is_allowed(update): return
        
        command = update.message.text
        try:
            plan = await self.planner.plan(command)
            results = []
            for step in plan.steps:
                result = await self.executor.execute(step)
                results.append(result)
                verified = await self.feedback.verify(step, result)
                if not verified.success:
                    recovery = await self.planner.recover(step, verified)
                    if recovery:
                        rec_res = await self.executor.execute(recovery)
                        
                # Check for screenshot in result to send as photo
                try:
                    if result and hasattr(result.output, "save"):
                        import tempfile
                        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                            result.output.save(f.name)
                            await update.message.reply_photo(photo=open(f.name, "rb"))
                        os.unlink(f.name)
                except Exception as e:
                    print(f"Error sending photo to Telegram: {e}")
                    
            summary = await self.planner.summarize(plan, results)
            self.memory.save_interaction(command, plan, results)
            await update.message.reply_text(summary)
            
        except Exception as e:
            await update.message.reply_text(f"Error processing command: {e}")
