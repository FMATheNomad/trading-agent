import asyncio
import signal
import httpx
import config
from dca_smart import SmartDCA, DCA_CONFIGS

shutdown_flag = False

def handle_sig(*_):
    global shutdown_flag
    shutdown_flag = True

async def main():
    global shutdown_flag
    signal.signal(signal.SIGTERM, handle_sig)
    signal.signal(signal.SIGINT, handle_sig)

    dca = SmartDCA()
    dca.load_instances()
    if dca.instances:
        print(f"Restored {len(dca.instances)} DCA instances from dca_state.json", flush=True)

    mode = "PAPER" if config.PAPER_TRADING else "LIVE"
    print("=" * 50, flush=True)
    print("  DCA SMART — STANDALONE", flush=True)
    print(f"  Mode: {mode}", flush=True)
    print(f"  Pairs: {', '.join(DCA_CONFIGS.keys())}")
    print("=" * 50, flush=True)

    loop_interval = 15

    async with httpx.AsyncClient(timeout=30) as client:
        while not shutdown_flag:
            try:
                await dca.run_cycle(client)
            except Exception as e:
                print(f"DCA cycle error: {e}", flush=True)
                import traceback
                traceback.print_exc()
            for _ in range(loop_interval):
                if shutdown_flag:
                    break
                await asyncio.sleep(1)

    print("DCA Smart shutdown complete.", flush=True)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nDCA Smart shutdown.", flush=True)
    except Exception as e:
        print(f"DCA fatal: {e}", flush=True)
        import traceback
        traceback.print_exc()
