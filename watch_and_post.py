# watch_and_post.py
# pip install watchdog requests

import time, json, logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# === CONFIG ===
WATCH_DIR = Path(r"D:\Image\62001FS04")        # <-- change
URL       = "http://10.226.52.32:8040/services/xRaySby/in"       # <-- change
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


LOG_FILE = Path(__file__).resolve().with_name("logs.txt")
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

def main():
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        observer = Observer()
        observer.schedule(Handler(pool), str(WATCH_DIR), recursive=True)
        observer.start()
        log(f"Watching directory {WATCH_DIR}")
        try:
            while True: time.sleep(1)
        except KeyboardInterrupt:
            observer.stop()
            log("Received shutdown signal, stopping observer", logging.INFO)
        observer.join()

if __name__ == "__main__":
    main()
