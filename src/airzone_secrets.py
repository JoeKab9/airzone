#!/usr/bin/env python3
"""
Airzone Secrets Manager
========================
Project-isolated credential storage using .env files.
Each project has its own .env — no cross-project access.

Usage:
    from airzone_secrets import secrets

    # Read a credential (loaded from .env)
    email = secrets.get("email")

    # Store a credential (writes back to .env)
    secrets.set("netatmo_access_token", "abc123")

    # Migrate from old config (one-time)
    secrets.migrate_from_config(cfg)
"""

import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

log = logging.getLogger("airzone.secrets")

# Keys we consider sensitive
SENSITIVE_KEYS = {
    "email",
    "password",
    "linky_token",
    "linky_prm",
    "netatmo_client_id",
    "netatmo_client_secret",
    "netatmo_access_token",
    "netatmo_refresh_token",
    "netatmo_token_expiry",
    "netatmo_token_obtained",
    "netatmo_token_expires_in",
    "airzone_token",
    "airzone_refresh_token",
    "airzone_token_expiry",
}

# Map between .env VAR_NAMES and internal key names
_KEY_TO_ENV = {
    "email": "AIRZONE_EMAIL",
    "password": "AIRZONE_PASSWORD",
    "linky_token": "LINKY_TOKEN",
    "linky_prm": "LINKY_PRM",
    "netatmo_client_id": "NETATMO_CLIENT_ID",
    "netatmo_client_secret": "NETATMO_CLIENT_SECRET",
    "netatmo_access_token": "NETATMO_ACCESS_TOKEN",
    "netatmo_refresh_token": "NETATMO_REFRESH_TOKEN",
    "netatmo_token_expiry": "NETATMO_TOKEN_EXPIRY",
    "netatmo_token_obtained": "NETATMO_TOKEN_OBTAINED",
    "netatmo_token_expires_in": "NETATMO_TOKEN_EXPIRES_IN",
    "airzone_token": "AIRZONE_TOKEN",
    "airzone_refresh_token": "AIRZONE_REFRESH_TOKEN",
    "airzone_token_expiry": "AIRZONE_TOKEN_EXPIRY",
}

_ENV_TO_KEY = {v: k for k, v in _KEY_TO_ENV.items()}


def _find_env_path() -> Path:
    """Find the project .env file. Walks up from this file's directory."""
    # Start from the src/ directory, look for .env in parent (project root)
    d = Path(__file__).resolve().parent
    for _ in range(5):
        candidate = d / ".env"
        if candidate.exists():
            return candidate
        # Also check if .env.example exists (means we're in the right dir)
        if (d / ".env.example").exists():
            return d / ".env"
        d = d.parent
    # Default: project root (parent of src/)
    return Path(__file__).resolve().parent.parent / ".env"


def _parse_env_file(path: Path) -> dict:
    """Parse a .env file into a dict of {VAR_NAME: value}."""
    data = {}
    if not path.exists():
        return data
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)=(.*)', line)
        if m:
            key = m.group(1)
            val = m.group(2).strip()
            # Remove surrounding quotes if present
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                val = val[1:-1]
            data[key] = val
    return data


def _write_env_file(path: Path, data: dict):
    """Write a dict back to .env file, preserving comments and order."""
    lines = []
    existing_keys = set()

    if path.exists():
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                lines.append(line)
                continue
            m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)=', stripped)
            if m:
                key = m.group(1)
                existing_keys.add(key)
                if key in data:
                    lines.append(f"{key}={data[key]}")
                else:
                    lines.append(line)
            else:
                lines.append(line)

    # Append any new keys not yet in the file
    for key, val in data.items():
        if key not in existing_keys and val:
            lines.append(f"{key}={val}")

    path.write_text("\n".join(lines) + "\n")
    # Restrict permissions (owner-only)
    try:
        os.chmod(str(path), 0o600)
    except Exception:
        pass


class SecretsManager:
    """Project-isolated credential storage using .env files."""

    def __init__(self):
        self._env_path: Optional[Path] = None
        self._cache: Optional[dict] = None
        self.using_keyring = False  # Compat flag — always False now

    def _ensure_loaded(self):
        if self._cache is None:
            self._env_path = _find_env_path()
            self._cache = _parse_env_file(self._env_path)
            # Also load into os.environ so libraries that use env vars work
            for env_key, val in self._cache.items():
                if val and env_key not in os.environ:
                    os.environ[env_key] = val
            if self._env_path.exists():
                log.debug("Loaded secrets from %s", self._env_path)
            else:
                log.debug("No .env file at %s — create from .env.example",
                          self._env_path)

    def get(self, key: str, default: str = "") -> str:
        """Retrieve a secret by internal key name."""
        self._ensure_loaded()
        env_name = _KEY_TO_ENV.get(key, key.upper())
        # Check os.environ first (allows runtime overrides)
        val = os.environ.get(env_name)
        if val:
            return val
        # Then check .env file cache
        val = self._cache.get(env_name)
        return val if val else default

    def set(self, key: str, value: str):
        """Store a secret. Writes to both .env file and os.environ."""
        self._ensure_loaded()
        env_name = _KEY_TO_ENV.get(key, key.upper())
        if not value:
            self.delete(key)
            return
        self._cache[env_name] = value
        os.environ[env_name] = value
        _write_env_file(self._env_path, self._cache)
        log.debug("Stored %s in .env", key)

    def delete(self, key: str):
        """Remove a secret from .env and os.environ."""
        self._ensure_loaded()
        env_name = _KEY_TO_ENV.get(key, key.upper())
        self._cache.pop(env_name, None)
        os.environ.pop(env_name, None)
        _write_env_file(self._env_path, self._cache)

    def get_all(self) -> dict:
        """Get all known secrets as a dict (for Settings dialog pre-fill)."""
        return {k: self.get(k) for k in SENSITIVE_KEYS}

    def migrate_from_config(self, cfg: dict, config_path: Path = None):
        """One-time migration: move sensitive keys from config to .env.
        Removes them from the config file afterwards."""
        migrated = []
        for key in SENSITIVE_KEYS & set(cfg.keys()):
            val = cfg.get(key, "")
            if val and not self.get(key):  # Don't overwrite existing .env values
                self.set(key, str(val))
                migrated.append(key)

        if migrated and config_path and config_path.exists():
            try:
                raw = json.loads(config_path.read_text())
                changed = False
                for key in migrated:
                    if key in raw and raw[key]:
                        raw[key] = ""
                        changed = True
                if changed:
                    config_path.write_text(json.dumps(raw, indent=2) + "\n")
                    log.info("Migrated %d secrets to .env, "
                             "cleared from config: %s", len(migrated), migrated)
            except Exception as e:
                log.warning("Could not clean config file: %s", e)

        return migrated

    def migrate_tokens(self, token_path: Path, prefix: str = "airzone"):
        """Migrate JWT/OAuth tokens from a JSON file to .env."""
        if not token_path.exists():
            return
        try:
            data = json.loads(token_path.read_text())
            mapping = {
                "token": f"{prefix}_token",
                "refreshToken": f"{prefix}_refresh_token",
                "expiry": f"{prefix}_token_expiry",
                "access_token": f"{prefix}_access_token",
                "refresh_token": f"{prefix}_refresh_token",
            }
            moved = 0
            for file_key, secret_key in mapping.items():
                val = data.get(file_key, "")
                if val:
                    self.set(secret_key, str(val))
                    moved += 1
            if moved:
                log.info("Migrated %d tokens from %s to .env",
                         moved, token_path.name)
                token_path.unlink()
                log.info("Deleted plaintext token file: %s", token_path)
        except Exception as e:
            log.warning("Token migration failed for %s: %s", token_path, e)

    @property
    def backend_name(self) -> str:
        """Human-readable name of the active storage backend."""
        self._ensure_loaded()
        if self._env_path and self._env_path.exists():
            return f".env file ({self._env_path})"
        return ".env file (not yet created)"


# Module-level singleton
secrets = SecretsManager()
