"""Load operator secrets from the environment or a git-ignored local file.

The Claude brain runs as a SINGLE server process that holds ONE Anthropic
API key. Users never receive a key — they reach the one console over
Google SSO, and that process makes the Anthropic calls. So a key only ever
has to exist in one place: the operator host's environment.

This loader looks, in order:
  1. the process environment (e.g. set by systemd, a launcher, 1Password,
     or `export`),
  2. ``$DSA_OPERATOR_SECRETS`` if set,
  3. ``~/.config/dsa110-operator/secrets.env``,
  4. ``.env`` in the repo root.

Files are simple ``KEY=VALUE`` lines (``#`` comments allowed). Existing
environment values are never overwritten. **No secret value is ever
logged** — only the names of the keys that were loaded. ``.env`` /
``*.env`` / ``.secrets/`` are git-ignored, so nothing lands in git.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Iterable

LOG = logging.getLogger("dsa_operator.env")

# Names treated as secrets for logging hygiene (only names are ever logged).
SECRET_NAMES = (
    "ANTHROPIC_API_KEY",
    "GOOGLE_CLIENT_SECRET",
    "DSA_OPERATOR_SECRET_KEY",
    "DSA_OPERATOR_SLACK_WEBHOOK_URL",
)


def _candidate_files() -> Iterable[Path]:
    explicit = os.environ.get("DSA_OPERATOR_SECRETS")
    if explicit:
        yield Path(explicit).expanduser()
    yield Path.home() / ".config" / "dsa110-operator" / "secrets.env"
    yield Path(__file__).resolve().parents[2] / ".env"


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            out[key] = val
    return out


def load_secrets() -> list[str]:
    """Populate os.environ from the first secrets file found. Idempotent.

    Returns the list of key NAMES newly set (never values), for logging.
    """
    loaded: list[str] = []
    for path in _candidate_files():
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        try:
            kv = _parse_env_file(path)
        except OSError as exc:
            LOG.warning("could not read secrets file %s: %s", path, exc)
            continue
        for key, val in kv.items():
            if key not in os.environ:          # never overwrite the real env
                os.environ[key] = val
                loaded.append(key)
        if loaded:
            LOG.info("loaded %d secret(s) from %s: %s", len(loaded), path,
                     ", ".join(sorted(loaded)))
        break                                   # first file wins
    return loaded


def have_anthropic_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


__all__ = ["load_secrets", "have_anthropic_key", "SECRET_NAMES"]
