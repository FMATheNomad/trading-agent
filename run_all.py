import asyncio
import signal
import sys

"""
run_all.py — Combined entry point for main bot + DCA Smart.
Runs both in a single process. Signal handling delegated to main.py.
"""

import config
import main as main_bot
from dca_smart import SmartDCA
import httpx

async def dca_loop(dca: SmartDCA):
    async with httpx.AsyncClient(timeout=30) as c:
        while not main_bot.shutdown_flag:
            try:
                await dca.run_cycle(c)
            except Exception as e:
                print(f"DCA error: {e}", flush=True)
                import traceback
                traceback.print_exc()
            for _ in range(15):
                if main_bot.shutdown_flag:
                    break
                await asyncio.sleep(1)

async def main():
    dca = SmartDCA()
    dca.load_instances()
    if dca.instances:
        print(f"Restored {len(dca.instances)} DCA instances", flush=True)

    dca_task = asyncio.create_task(dca_loop(dca))
    main_task = asyncio.create_task(main_bot.main())

    done, pending = await asyncio.wait(
        [main_task, dca_task],
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
    print("Shutdown complete.", flush=True)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutdown.", flush=True)
    except Exception as e:
        print(f"Fatal: {e}", flush=True)
        import traceback
        traceback.print_exc()
