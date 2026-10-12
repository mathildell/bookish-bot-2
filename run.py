"""Script entrypoint: python run.py"""

import asyncio

from bot.main import run

if __name__ == "__main__":
    asyncio.run(run())