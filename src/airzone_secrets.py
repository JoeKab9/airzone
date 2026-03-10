#!/usr/bin/env python3
"""
Airzone Secrets Manager
========================
Secure credential storage using the OS keychain:
  - macOS: Keychain Access (encrypted at rest)
  - Windows: Windows Credential Manager (DPAPI-encrypted)
  - Linux: Secret Service (GNOME Keyring / KDE Wallet)

Falls back to plaintext JSON if keyring is unavailable.

Usage:
    from airzone_secrets import secrets

    # Store a credential
    secrets.set("email", "user@example.com")

    # Retrieve it
    email = secrets.get("email")

    # Migrate from old config (one-time)
    secrets.migrate_from_config(cfg)
"""

import json
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger("airzone.secrets")

SERVICE_NAME = "Airzone"

# Keys we consider sensitive and want in the keychain
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
    "airzone_token",
    "airzone_refresh_token",
    "airzone_token_expiry",
}

# Try to import keyring; fall back to file-based storage
try:
    import keyring as _keyring
    # Quick test to make sure the backend works
    _keyring.get_credential(SERVICE_NAME, None)
    HAS_KEYRING = True
    _backend_name = type(_keyring.get_keyring()).__name__
    log.debug("Keyring backend: %s", _backend_name)
except Exception:
    HAS_KEYRING = False
    _backend_name = "none"
    log.debug("Keyring not available — falling back to file storage")


class _FallbackStore:
    """Encrypted-ish file store for when keyring isn't available.
    Stores in a restricted-permissions JSON file."""

    def __init__(self, path: Path):
        self.path = path
        self._data: dict = {}
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text())
            except Exception:
                self._data = {}

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2) + "\n")
        # Restrict permissions (owner-only read/write)
        try:
            import os, stat
            os.chmod(str(self.path), stat.S_IRUSR | stat.S_IWUSR)
        except Exception:
            pass

    def get(self, key: str) -> Optional[str]:
        return self._data.get(key)

    def set(self, key: str, value: str):
        self._data[key] = value
        self._save()

    def delete(self, key: str):
        if key in self._data:
            del self._data[key]
            self._save()


class SecretsManager:
    """Unified interface for credential storage."""

    def __init__(self):
        self._fallback: Optional[_FallbackStore] = None
        self.using_keyring = HAS_KEYRING

    def _get_fallback(self) -> _FallbackStore:
        """Lazy-init fallback store (needs DATA_DIR which isn't set at import)."""
        if self._fallback is None:
            from airzone_humidity_controller import DATA_DIR
            fb_path = DATA_DIR / ".airzone_secrets.json"
            self._fallback = _FallbackStore(fb_path)
        return self._fallback

    def get(self, key: str, default: str = "") -> str:
        """Retrieve a secret by key. Returns default if not found."""
        val = None
        if self.using_keyring:
            try:
                val = _keyring.get_password(SERVICE_NAME, key)
            except Exception as e:
                log.debug("Keyring read failed for %s: %s", key, e)
        if val is None:
            val = self._get_fallback().get(key)
        return val if val is not None else default

    def set(self, key: str, value: str):
        """Store a secret. Empty/None values are deleted."""
        if not value:
            self.delete(key)
            return
        if self.using_keyring:
            try:
                _keyring.set_password(SERVICE_NAME, key, value)
                log.debug("Stored %s in keychain", key)
                return
            except Exception as e:
                log.warning("Keyring write failed for %s: %s — using fallback", key, e)
        self._get_fallback().set(key, value)

    def delete(self, key: str):
        """Remove a secret."""
        if self.using_keyring:
            try:
                _keyring.delete_password(SERVICE_NAME, key)
            except Exception:
                pass
        try:
            self._get_fallback().delete(key)
        except Exception:
            pass

    def get_all(self) -> dict:
        """Get all known secrets as a dict (for Settings dialog pre-fill)."""
        return {k: self.get(k) for k in SENSITIVE_KEYS}

    def migrate_from_config(self, cfg: dict, config_path: Path = None):
        """One-time migration: move sensitive keys from config to secure storage.
        Removes them from the config file afterwards."""
        migrated = []
        for key in SENSITIVE_KEYS & set(cfg.keys()):
            val = cfg.get(key, "")
            if val:
                self.set(key, str(val))
                migrated.append(key)

        if migrated and config_path and config_path.exists():
            # Re-read and strip sensitive values from the config file
            try:
                raw = json.loads(config_path.read_text())
                changed = False
                for key in migrated:
                    if key in raw and raw[key]:
                        raw[key] = ""
                        changed = True
                if changed:
                    config_path.write_text(json.dumps(raw, indent=2) + "\n")
                    log.info("Migrated %d secrets to secure storage, "
                             "cleared from config: %s", len(migrated), migrated)
            except Exception as e:
                log.warning("Could not clean config file: %s", e)

        return migrated

    def migrate_tokens(self, token_path: Path, prefix: str = "airzone"):
        """Migrate JWT/OAuth tokens from a JSON file to secure storage."""
        if not token_path.exists():
            return
        try:
            data = json.loads(token_path.read_text())
            mapping = {
                "token": f"{prefix}_token",
                "refreshToken": f"{prefix}_refresh_token",
                "expiry": f"{prefix}_token_expiry",
                # Netatmo-style keys
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
                log.info("Migrated %d tokens from %s to secure storage",
                         moved, token_path.name)
                # Remove the plaintext token file
                token_path.unlink()
                log.info("Deleted plaintext token file: %s", token_path)
        except Exception as e:
            log.warning("Token migration failed for %s: %s", token_path, e)

    @property
    def backend_name(self) -> str:
        """Human-readable name of the active storage backend."""
        if self.using_keyring:
            return f"OS Keychain ({_backend_name})"
        return "Encrypted file (fallback)"


# Module-level singleton
secrets = SecretsManager()
