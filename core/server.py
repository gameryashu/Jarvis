import asyncio
from aiohttp import web

from config.settings import Settings
from core.memory import MemoryManager
from core.llm import LLMPlanner
from core.executor import ActionExecutor
from core.feedback import FeedbackLoop


class JarvisServer:
    """A persistent HTTP server to accept JSON commands for JARVIS"""

    def __init__(self, settings: Settings, loop_fn):
        self.settings = settings
        self.loop_fn = loop_fn
        
        # Keep instances alive between requests
        self.memory = MemoryManager(settings)
        self.planner = LLMPlanner(settings, self.memory)
        self.executor = ActionExecutor(settings)
        self.feedback = FeedbackLoop(settings, self.executor)

    async def handle_health(self, request):
        return web.json_response({"status": "healthy"})

    async def handle_command(self, request):
        try:
            data = await request.json()
            command = data.get("command", "")
            if not command:
                return web.json_response({"success": False, "error": "No command provided"}, status=400)
            
            print(f"\n[Server] Received command: {command}")
            summary = await self.loop_fn(
                goal=command,
                planner=self.planner,
                executor=self.executor,
                feedback=self.feedback,
                memory=self.memory,
                max_iterations=self.settings.autonomous_max_iterations,
            )
            return web.json_response({"success": True, "result": summary})
        except Exception as e:
            print(f"[Server Error] {e}")
            return web.json_response({"success": False, "error": str(e)}, status=500)


async def start_server(settings: Settings, loop_fn):
    server = JarvisServer(settings, loop_fn)
    app = web.Application()
    app.router.add_post('/command', server.handle_command)
    app.router.add_get('/health', server.handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, 'localhost', 7429)
    await site.start()
    print("JARVIS server running on http://localhost:7429")

    # Keep alive
    while True:
        await asyncio.sleep(3600)
