# config_manager.py

"""
Lightweight configuration management for the XML watcher application.
Stores user settings (watch directory and endpoint URL) in a JSON file
next to the scripts so both CLI and GUI flows share the same values.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Dict, Any

DEFAULT_FTP_BASE = "import"
VALID_FTP_BASES = {"import", "export"}

DEFAULT_CONFIG: Dict[str, Any] = {
    "watch_dir": r"D:\Image\62001FS04",
    "url": "http://10.226.52.32:8040/services/xRaySby/in",
    "ftp_base": DEFAULT_FTP_BASE,
}

def get_app_base_dir() -> Path:
    """
    Returns the directory that should hold runtime artifacts like settings and logs.
    When running from a PyInstaller executable we use the executable's directory.
    Otherwise we fall back to the source directory.
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "executable"):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_BASE_DIR = get_app_base_dir()
CONFIG_FILE = APP_BASE_DIR / "settings.json"
_LOCK = threading.RLock()


def _merge_with_defaults(data: Dict[str, Any]) -> Dict[str, Any]:
    """Returns a config dict that always has the expected keys."""
    merged = DEFAULT_CONFIG.copy()
    for key, value in data.items():
        if value is None:
            continue
        if key == "ftp_base":
            candidate = str(value).strip().lower()
            merged[key] = candidate if candidate in VALID_FTP_BASES else DEFAULT_FTP_BASE
            continue
        merged[key] = value
    return merged


def load_config() -> Dict[str, Any]:
    """
    Loads configuration from disk. If the file is missing or invalid
    the default configuration is returned and persisted.
    """
    with _LOCK:
        if not CONFIG_FILE.exists():
            save_config(DEFAULT_CONFIG.copy())
            return DEFAULT_CONFIG.copy()
        try:
            loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("Configuration root must be an object")
        except Exception:
            # Backup the invalid configuration for inspection and restore defaults
            backup = CONFIG_FILE.with_suffix(".json.bak")
            try:
                CONFIG_FILE.replace(backup)
            except OSError:
                pass
            save_config(DEFAULT_CONFIG.copy())
            return DEFAULT_CONFIG.copy()

        merged = _merge_with_defaults(loaded)
        # Persist merged config to capture any new defaults
        save_config(merged)
        return merged


def save_config(config: Dict[str, Any]) -> None:
    """Persists validated configuration to disk."""
    with _LOCK:
        normalized = _merge_with_defaults(config)
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(normalized, indent=2), encoding="utf-8")


def update_config(**updates: Any) -> Dict[str, Any]:
    """
    Applies updates to the current configuration and saves them.
    Returns the new configuration dictionary.
    """
    with _LOCK:
        current = load_config()
        current.update({k: v for k, v in updates.items() if v is not None})
        save_config(current)
        return current
