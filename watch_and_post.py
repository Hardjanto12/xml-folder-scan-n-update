# watch_and_post.py
# pip install watchdog requests

import time, json, shutil
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

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
PROCESSED_DIR = WATCH_DIR / "_processed"
FAILED_DIR    = WATCH_DIR / "_failed"
AUDIT_JSON    = WATCH_DIR / "_audit_json"    # optional: keep a copy of posted JSON

# === IMPORT YOUR CONVERTER PRIMITIVES ===
# Save your script as idr_xml_to_json.py in the same folder as this file.
from convert_idr_xml import detect_and_read, strip_ns, build_image_msg
from xml.etree import ElementTree as ET

session = requests.Session()
session.headers.update(API_HEADERS)

for p in (PROCESSED_DIR, FAILED_DIR, AUDIT_JSON):
    p.mkdir(exist_ok=True, parents=True)

def wait_until_stable(path: Path, window=STABLE_WAIT_S, tries=3) -> bool:
    last = -1
    for _ in range(tries):
        if not path.exists():
            return False
        size = path.stat().st_size
        if size == last:
            return True
        last = size
        time.sleep(window)
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
    return session.post(URL, json=payload, timeout=TIMEOUT_S)

def handle_xml(path: Path):
    # 1) stability (avoid partial writes)
    if not wait_until_stable(path):
        return

    # 2) convert using your code
    try:
        payload = convert_xml_to_payload(path)
    except Exception as e:
        print(f"[CONVERT-ERR] {path.name}: {e}")
        shutil.move(str(path), str(FAILED_DIR / path.name))
        return

    # 3) POST (with small retry/backoff)
    attempts = 0
    backoff = 2
    while True:
        attempts += 1
        try:
            resp = send_payload(payload)
            if 200 <= resp.status_code < 300:
                # 4) audit json (optional)
                try:
                    (AUDIT_JSON / (path.stem + ".json")).write_text(
                        json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8"
                    )
                except Exception:
                    pass
                shutil.move(str(path), str(PROCESSED_DIR / path.name))
                print(f"[OK] {path.name} -> {resp.status_code}")
                return
            else:
                print(f"[POST-WARN] {path.name} -> HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            print(f"[POST-ERR] {path.name} (attempt {attempts}): {e}")

        if attempts >= 5:
            shutil.move(str(path), str(FAILED_DIR / path.name))
            # keep a copy of the JSON that failed
            try:
                (AUDIT_JSON / (path.stem + ".json")).write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8"
                )
            except Exception:
                pass
            print(f"[FAIL] {path.name} moved to _failed")
            return
        time.sleep(min(backoff, 30))
        backoff *= 2

class Handler(FileSystemEventHandler):
    def __init__(self, executor): self.executor = executor
    def _maybe_submit(self, p: Path):
        if p.suffix.lower() == ".xml":
            print(f"[EVENT] {p.name}")
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
        print(f"Watching: {WATCH_DIR}")
        try:
            while True: time.sleep(1)
        except KeyboardInterrupt:
            observer.stop()
        observer.join()

if __name__ == "__main__":
    main()
