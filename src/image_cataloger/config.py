"""API key storage (OS keychain via keyring, or environment) and run settings."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

KEYRING_SERVICE = "image-cataloger"
KEYRING_USER = "anthropic_api_key"
ENV_VAR = "ANTHROPIC_API_KEY"


def get_api_key() -> str | None:
    """Environment variable wins, then the OS keychain."""
    env = os.environ.get(ENV_VAR)
    if env:
        return env.strip()
    try:
        import keyring

        value = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
        return value.strip() if value else None
    except Exception:
        return None


def set_api_key(value: str | None) -> None:
    """Store (or with None/empty, delete) the key in the OS keychain."""
    import keyring

    if value and value.strip():
        keyring.set_password(KEYRING_SERVICE, KEYRING_USER, value.strip())
    else:
        try:
            keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
        except keyring.errors.PasswordDeleteError:
            pass


def key_source() -> str:
    if os.environ.get(ENV_VAR):
        return "environment variable"
    return "keychain" if get_api_key() else "not set"


def default_output_base(folder: str | os.PathLike) -> Path:
    """Default location for the DB/CSV/JSON: next to the chosen folder.

    Falls back to ~/ImageCataloger when the parent is not writable (e.g. /Volumes).
    """
    folder = Path(folder).expanduser().resolve()
    parent = folder.parent
    base_name = folder.name or "catalog"
    if os.access(parent, os.W_OK) and parent != Path("/Volumes"):
        return parent / f"{base_name}_catalog"
    fallback = Path.home() / "ImageCataloger"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback / f"{base_name}_catalog"


@dataclass
class RunConfig:
    folder: str
    recursive: bool = True
    workers: int = 4
    batch_size: int = 6
    engine: str = "api"  # "api" or "cli"
    model: str = "claude-haiku-5-5"
    effort: str = "low"
    max_side: int = 1568
    jpeg_quality: int = 85
    db_path: str = ""
    output_csv: str = ""
    output_json: str = ""
    output_duplicates_csv: str = ""
    dedupe_only: bool = False  # hash, link duplicates, export; never call the model
    include_videos: bool = True
    video_frames: int = 5  # frames sampled per video for the description
    api_key: str | None = None
    request_timeout: float = 120.0
    max_attempts: int = 8
    extra: dict = field(default_factory=dict)

    def resolve_paths(self) -> None:
        base = default_output_base(self.folder)
        if not self.db_path:
            self.db_path = str(base.with_suffix(".sqlite"))
        if not self.output_csv:
            self.output_csv = str(base.with_suffix(".csv"))
        if not self.output_json:
            self.output_json = str(base.with_suffix(".json"))
        if not self.output_duplicates_csv:
            self.output_duplicates_csv = str(base.parent / f"{base.name}_duplicates.csv")
