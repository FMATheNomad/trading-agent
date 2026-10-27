import asyncio
import sys

"""
run_all.py — Production entry point.

main.main() already runs the SmartDCA engine internally (_dca_task),
so this wrapper only delegates to it. A second SmartDCA here would run
two engines concurrently writing the same dca_state.json and placing
duplicate base orders.
"""

import main as main_bot


if __name__ == "__main__":
    try:
        asyncio.run(main_bot.main())
    except KeyboardInterrupt:
        print("\nShutdown.", flush=True)
    except Exception as e:
        print(f"Fatal: {e}", flush=True)
        import traceback
        traceback.print_exc()
        sys.exit(1)
