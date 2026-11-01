"""Bot commands (``/bot ...``).

Root group that hosts the ``say``, ``edit`` and ``react`` subcommands.
"""

from __future__ import annotations

from discord import app_commands

from ..texts import t
from .bot_edit import edit_command
from .bot_react import react_command
from .bot_say import say_command

bot_group = app_commands.Group(
    name="bot",
    description=t("bot.group_description"),
)
bot_group.add_command(say_command)
bot_group.add_command(edit_command)
bot_group.add_command(react_command)
