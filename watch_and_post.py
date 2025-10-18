# watch_and_post.py
# pip install watchdog requests

import time, json, logging, threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Optional, Dict, Any

import requests
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from config_manager import load_config, get_app_base_dir

# === CONFIG ===
API_HEADERS = {
    "Content-Type": "application/json",      # server expects JSON with {"ftp_path","image_msg"}
    # "Authorization": "Bearer <token>",     # if needed
}
MAX_WORKERS   = 2
STABLE_WAIT_S = 1.2       # size unchanged window
TIMEOUT_S     = 20

# === IMPORT YOUR CONVERTER PRIMITIVES ===
# Save your script as idr_xml_to_json.py in the same folder as this file.
from convert_idr_xml import detect_and_read, strip_ns, build_image_msg
from xml.etree import ElementTree as ET

session = requests.Session()
session.headers.update(API_HEADERS)


APP_BASE_DIR = get_app_base_dir()
LOGS_DIR = APP_BASE_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOGS_DIR / "logs.txt"
try:
    JAKARTA_TZ = ZoneInfo("Asia/Jakarta")
except ZoneInfoNotFoundError:
    JAKARTA_TZ = timezone(timedelta(hours=7))


class JakartaFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=JAKARTA_TZ)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.isoformat(timespec="seconds")


def configure_logger() -> logging.Logger:
    logger = logging.getLogger("watch_and_post")
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=15 * 1024 * 1024,
        backupCount=4,
        encoding="utf-8"
    )
    formatter = JakartaFormatter(
        fmt="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)

    logger.propagate = False
    return logger


logger = configure_logger()

_config_cache: Dict[str, Any] = load_config()
WATCH_DIR = Path(_config_cache["watch_dir"])
URL = _config_cache["url"]


def reload_runtime_config() -> Dict[str, Any]:
    """Reloads configuration from disk and updates module-level globals."""
    global _config_cache, WATCH_DIR, URL
    _config_cache = load_config()
    WATCH_DIR = Path(_config_cache["watch_dir"])
    URL = _config_cache["url"]
    log(f"Configuration reloaded: watch_dir={WATCH_DIR}, url={URL}")
    return _config_cache.copy()


def current_config() -> Dict[str, Any]:
    """Returns the cached configuration (path converted to string)."""
    return {
        "watch_dir": str(WATCH_DIR),
        "url": URL,
    }


def log(message: str, level: int = logging.INFO, *args, **kwargs) -> None:
    logger.log(level, message, *args, **kwargs)


def wait_until_stable(path: Path, window=STABLE_WAIT_S, tries=3) -> bool:
    last = -1
    for _ in range(tries):
        if not path.exists():
            log(f"File {path} no longer exists during stability check", logging.WARNING)
            return False
        size = path.stat().st_size
        if size == last:
            return True
        last = size
        time.sleep(window)
    log(f"File {path} did not stabilize after {tries} checks", logging.WARNING)
    return True

def convert_xml_to_payload(xml_path: Path) -> dict:
    """
    Uses your converter logic IN-MEMORY (no extra disk IO).
    Returns: {"ftp_path": "...", "image_msg": "<IDR>...</IDR>"}
    """
    xml_text = detect_and_read(xml_path)
    root = ET.fromstring(strip_ns(xml_text))
    ftp_path, image_msg_xml = build_image_msg(root)
    return {"ftp_path": ftp_path, "image_msg": image_msg_xml}

def send_payload(payload: dict) -> requests.Response:
    log(f"Sending payload: {json.dumps(payload, ensure_ascii=False)}")
    return session.post(URL, json=payload, timeout=TIMEOUT_S)

def handle_xml(path: Path):
    # 1) stability (avoid partial writes)
    if not wait_until_stable(path):
        log(f"Skipping {path.name} because file never stabilized", logging.WARNING)
        return

    # 2) convert using your code
    log(f"Starting processing for {path.name}")
    try:
        log(f"Converting {path.name} to payload")
        payload = convert_xml_to_payload(path)
    except Exception:
        logger.exception("Failed to convert %s", path.name)
        log(f"{path.name} left in place due to conversion error", logging.ERROR)
        return

    # 3) POST (with small retry/backoff)
    attempts = 0
    backoff = 2
    while True:
        attempts += 1
        try:
            log(f"Attempt {attempts} sending payload for {path.name}")
            resp = send_payload(payload)
            if 200 <= resp.status_code < 300:
                log(
                    f"{path.name} processed successfully with status {resp.status_code}"
                )
                return
            else:
                log(
                    f"{path.name} returned HTTP {resp.status_code}: {resp.text[:200]}",
                    logging.WARNING
                )
        except Exception:
            logger.exception("Error posting %s on attempt %s", path.name, attempts)

        if attempts >= 5:
            log(
                f"{path.name} exhausted retries and remains in place for manual review",
                logging.ERROR
            )
            return
        time.sleep(min(backoff, 30))
        backoff *= 2

class Handler(FileSystemEventHandler):
    def __init__(self, executor): self.executor = executor
    def _maybe_submit(self, p: Path):
        if p.suffix.lower() == ".xml":
            log(f"Detected new XML file {p.name}")
            self.executor.submit(handle_xml, p)
    def on_created(self, event):
        if not event.is_directory: self._maybe_submit(Path(event.src_path))
    def on_moved(self, event):
        if not event.is_directory: self._maybe_submit(Path(event.dest_path))

class WatchService:
    """
    Controls the watchdog observer lifecycle so that consumers (like a GUI)
    can start and stop the watcher on demand.
    """
    def __init__(self, watch_dir: Optional[Path | str] = None):
        self._watch_dir = Path(watch_dir) if watch_dir else Path(WATCH_DIR)
        self._executor = None
        self._observer = None
        self._lock = threading.Lock()

    @property
    def watch_dir(self) -> Path:
        return self._watch_dir

    def set_watch_dir(self, watch_dir: Path | str, restart: bool = True) -> bool:
        """
        Updates the watch directory. If the service is running it will restart
        when restart=True (default).
        """
        new_path = Path(watch_dir)
        with self._lock:
            running = self._observer is not None and self._observer.is_alive()
        if running:
            self.stop()
        with self._lock:
            self._watch_dir = new_path
        log(f"Watcher directory updated to {new_path}")
        success = True
        if restart and running:
            success = self.start()
        return success

    def refresh_from_config(self, restart: bool = True) -> bool:
        """Synchronizes the watch directory with the current module configuration."""
        return self.set_watch_dir(Path(WATCH_DIR), restart=restart)

    def is_running(self) -> bool:
        with self._lock:
            return self._observer is not None and self._observer.is_alive()

    def start(self) -> bool:
        with self._lock:
            if self._observer is not None and self._observer.is_alive():
                log("Watcher already running", logging.INFO)
                return False
            executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
            if not self._watch_dir.exists():
                executor.shutdown(wait=False)
                log(f"Watch directory {self._watch_dir} does not exist", logging.ERROR)
                return False
            observer = Observer()
            handler = Handler(executor)
            try:
                observer.schedule(handler, str(self._watch_dir), recursive=True)
                observer.start()
            except Exception:
                executor.shutdown(wait=False)
                logger.exception("Failed to start observer for %s", self._watch_dir)
                raise
            self._executor = executor
            self._observer = observer
        log(f"Watching directory {self._watch_dir}")
        return True

    def stop(self) -> bool:
        with self._lock:
            observer = self._observer
            executor = self._executor
            self._observer = None
            self._executor = None
        if observer is None:
            return False
        try:
            observer.stop()
            observer.join(timeout=5)
            if observer.is_alive():
                log("Observer did not stop within timeout", logging.WARNING)
        finally:
            if executor is not None:
                executor.shutdown(wait=False)
        log("Watcher stopped")
        return True

def main():
    service = WatchService()
    try:
        started = service.start()
    except Exception:
        logger.exception("Unable to start watch service")
        return
    if not started:
        return
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        log("Received shutdown signal, stopping observer", logging.INFO)
    finally:
        service.stop()

if __name__ == "__main__":
    main()
