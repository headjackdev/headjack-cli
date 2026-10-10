"""Tank's configuration: environment-driven, validated once at startup.

Startup must never contact Headjack — this module only reads and validates
local configuration. The first network call happens inside a tool invocation.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel

# Default resident-skills path for Claude Code-style harnesses.
_DEFAULT_SKILLS_ROOT = Path.home() / ".claude" / "skills"
_DEFAULT_STORE = Path.home() / ".headjack" / "cache"
_DEFAULT_MIRROR_PATH = Path.home() / ".headjack" / "mirror.db"


class TankConfigError(RuntimeError):
    """Raised for a configuration problem — a missing token, an unreadable path —
    that should fail Tank's startup loudly rather than surface later as a confusing
    tool-call error."""


class TankConfig(BaseModel):
    headjack_url: str = "https://api.headjack.dev"
    token: str  # from TANK_TOKEN
    store: Path = _DEFAULT_STORE
    skills_root: Path = _DEFAULT_SKILLS_ROOT
    mirror_path: Path = _DEFAULT_MIRROR_PATH
    mirror_ttl_hours: float = 6.0
    timeout_s: float = 5.0
    max_active: int = 50
    stale_days: int = 30

    @classmethod
    def from_env(cls) -> TankConfig:
        """Build config from the environment. Raises `TankConfigError` (naming the
        variable) rather than letting pydantic's validation error be the first thing
        an operator sees for a missing token."""
        token = os.environ.get("TANK_TOKEN")
        if not token:
            raise TankConfigError(
                "TANK_TOKEN is not set. Tank needs a Headjack API token to search — "
                "set the TANK_TOKEN environment variable before starting the server."
            )

        kwargs: dict[str, Any] = {"token": token}
        if url := os.environ.get("TANK_HEADJACK_URL"):
            kwargs["headjack_url"] = url
        if store := os.environ.get("TANK_STORE"):
            kwargs["store"] = Path(store)
        if skills_root := os.environ.get("TANK_SKILLS_ROOT"):
            kwargs["skills_root"] = Path(skills_root)
        if mirror_path := os.environ.get("TANK_MIRROR_PATH"):
            kwargs["mirror_path"] = Path(mirror_path)
        if mirror_ttl := os.environ.get("TANK_MIRROR_TTL_HOURS"):
            kwargs["mirror_ttl_hours"] = float(mirror_ttl)
        if timeout := os.environ.get("TANK_TIMEOUT_S"):
            kwargs["timeout_s"] = float(timeout)

        # model_validate (not cls(**kwargs)) because kwargs is a plain dict built up
        # conditionally — mypy can't verify a generic dict's values against each
        # field's specific type the way it can a literal keyword call.
        return cls.model_validate(kwargs)
