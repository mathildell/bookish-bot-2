"""Configuration loader.

Reads every secret from a single YAML file (config.yaml). Top-level keys are
exposed as attributes::

    config.discord_token
    config.guild_id
    config.mysql["host"]
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT_DIR / "config.yaml"
EXAMPLE_CONFIG_PATH = ROOT_DIR / "config.example.yaml"


class Config:
    def __init__(self, data: dict | None) -> None:
        data = data or {}
        for key, value in data.items():
            setattr(self, key, value)

    def get(self, name: str, default=None):
        return getattr(self, name, default)

    @property
    def mysql_url(self) -> str:
        return (
            f"mysql+aiomysql://{self.mysql['username']}:{self.mysql['password']}"
            f"@{self.mysql['host']}:{self.mysql['port']}/{self.mysql['database']}?charset=utf8mb4"
        )


@lru_cache(maxsize=1)
def load_config(path: Path | str = CONFIG_PATH) -> Config:
    if not Path(path).exists():
        raise FileNotFoundError(
            f"Config file not found at {path}. "
            f"Copy {EXAMPLE_CONFIG_PATH} to {path} and fill in your values."
        )
    with open(path, encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    return Config(data)