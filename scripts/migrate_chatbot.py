"""Migrate chatbot settings to the new schema.

Copies any existing ``chat_system_prompt`` into ``chat_persona``, then drops
the ``chat_system_prompt`` column. Run after deploying the code changes.

    .venv/bin/python scripts/migrate_chatbot.py
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.config import load_config  # noqa: E402
from bot.db import close_db, create_tables, init_db, session_scope  # noqa: E402


async def run(args: argparse.Namespace) -> None:
    config = load_config(Path(args.config))
    init_db(config.mysql_url)
    await create_tables()

    async with session_scope() as session:
        exists = (
            await session.execute(text("SHOW COLUMNS FROM bot_settings LIKE 'chat_system_prompt'"))
        ).first()
        if exists:
            row = (
                await session.execute(
                    text("SELECT chat_system_prompt FROM bot_settings WHERE chat_system_prompt IS NOT NULL LIMIT 1")
                )
            ).first()
            if row and row[0]:
                await session.execute(
                    text(
                        "UPDATE bot_settings SET chat_persona = COALESCE(chat_persona, :prompt) WHERE chat_persona IS NULL"
                    ),
                    {"prompt": row[0]},
                )
            await session.execute(text("ALTER TABLE bot_settings DROP COLUMN chat_system_prompt"))
            await session.commit()
            print("chat_system_prompt migrated to chat_persona and dropped")
        else:
            print("chat_system_prompt column not present")

    await close_db()


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate chatbot settings")
    parser.add_argument("-c", "--config", default="config.yaml", help="Path to config.yaml")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
