"""Launch the Gateway server: python gateway/main.py."""

import asyncio
import sys

from runtime import Gateway


for stream in (sys.stdin, sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")


gateway = Gateway()


async def main():
    await gateway.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nGateway stopped.")
