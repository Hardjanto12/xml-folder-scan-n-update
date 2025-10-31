# watcher_gui.py
# Tkinter interface for watch_and_post.py

import logging
import socket
import sys
import threading
import tkinter as tk
from pathlib import Path
from typing import Optional
from tkinter import ttk, messagebox, filedialog
from tkinter.scrolledtext import ScrolledText

import watch_and_post as backend
from config_manager import update_config, CONFIG_FILE

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    pystray = None
    Image = None
    ImageDraw = None

LOG_FILE = backend.LOG_FILE
JakartaFormatter = backend.JakartaFormatter
WatchService = backend.WatchService
log = backend.log
logger = backend.logger
reload_runtime_config = backend.reload_runtime_config
current_config = backend.current_config


SINGLE_INSTANCE_PORT = 52321
_SINGLE_INSTANCE_SOCKET: Optional[socket.socket] = None


def acquire_single_instance() -> bool:
    global _SINGLE_INSTANCE_SOCKET
    if _SINGLE_INSTANCE_SOCKET is not None:
        return True

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform != "win32":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
        sock.listen(1)
    except OSError:
        sock.close()
        return False

    _SINGLE_INSTANCE_SOCKET = sock
    return True


def release_single_instance() -> None:
    global _SINGLE_INSTANCE_SOCKET
    sock = _SINGLE_INSTANCE_SOCKET
    if sock is None:
        return
    try:
        sock.close()
    except OSError:
        pass
    _SINGLE_INSTANCE_SOCKET = None


def ensure_single_instance() -> bool:
    if acquire_single_instance():
        return True
    tmp_root: Optional[tk.Tk] = None
    try:
        tmp_root = tk.Tk()
        tmp_root.withdraw()
        messagebox.showerror(
            "Already Running",
            "Another instance of XML Watcher Control Center is already running.",
            parent=tmp_root,
        )
    except Exception:
        print(
            "Another instance of XML Watcher Control Center is already running.",
            file=sys.stderr,
        )
    finally:
        if tmp_root is not None:
            try:
                tmp_root.destroy()
            except Exception:
                pass
    return False


class TextHandler(logging.Handler):
    """
    Logging handler that forwards log records into a Tkinter Text widget.
    """

    LEVEL_TAGS = {
        logging.DEBUG: "logDebug",
        logging.INFO: "logInfo",
        logging.WARNING: "logWarning",
        logging.ERROR: "logError",
        logging.CRITICAL: "logCritical",
    }

    def __init__(self, widget: ScrolledText, formatter: logging.Formatter, max_lines: int = 2000):
        super().__init__()
        self.widget = widget
        self.max_lines = max_lines
        self.setFormatter(formatter)
        self._configure_tags()

    def _configure_tags(self) -> None:
        self.widget.tag_configure("logDebug", foreground="#4c51bf")
        self.widget.tag_configure("logInfo", foreground="#1f2933")
        self.widget.tag_configure("logWarning", foreground="#b7791f")
        self.widget.tag_configure("logError", foreground="#b91c1c")
        self.widget.tag_configure("logCritical", foreground="#7f1d1d", font=("Consolas", 10, "bold"))

    def emit(self, record: logging.LogRecord) -> None:
        message = self.format(record)
        tag = self.LEVEL_TAGS.get(record.levelno, "logInfo")
        self.widget.after(0, lambda: self._append(message, tag))

    def _append(self, message: str, tag: str) -> None:
        self.widget.insert("end", message + "\n", tag)
        line_count = int(self.widget.index("end-1c").split(".")[0])
        if line_count > self.max_lines:
            self.widget.delete("1.0", "2.0")
        self.widget.see("end")


class WatcherApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("XML Watcher Control Center")
        self.root.geometry("900x600")
        self.root.minsize(780, 520)
        # Default font for a clean office look (brace spaces for Tk)
        self.root.option_add("*Font", "{Segoe UI} 10")

        reload_runtime_config()
        self._config = current_config()

        self.service = WatchService(self._config["watch_dir"])
        self.status_var = tk.StringVar(value="Status: Stopped")
        self.watch_info_var = tk.StringVar(value=f"Watching directory: {self.service.watch_dir}")
        self.url_info_var = tk.StringVar(value=f"POST endpoint: {self._config['url']}")
        ftp_base = self._config.get("ftp_base", "import")
        self.ftp_info_var = tk.StringVar(value=f"FTP root: /{ftp_base}")
        self.settings_watch_dir_var = tk.StringVar(value=self._config["watch_dir"])
        self.settings_url_var = tk.StringVar(value=self._config["url"])
        self.settings_ftp_base_var = tk.StringVar(value=ftp_base)

        self._status_job = None
        self._tray_icon = None
        self._tray_thread = None
        self._tray_image = None
        self._tray_supported = pystray is not None and Image is not None and ImageDraw is not None
        self._tray_hint_shown = False
        self._closing = False

        self._bg_color = "#f4f6fb"
        self._accent_color = "#1f3c88"
        self.root.configure(background=self._bg_color)

        self._build_styles()
        self._build_layout()

        formatter = self._resolve_formatter()
        self.text_handler = TextHandler(self.log_display, formatter)
        logger.addHandler(self.text_handler)

        self._load_existing_logs()
        self._apply_status()
        self._schedule_status_refresh()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<Unmap>", self._on_unmap, add="+")
        self.root.bind("<Map>", self._on_map, add="+")
        self.root.after_idle(self._auto_start_on_launch)

    def _build_styles(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure("Office.TFrame", background=self._bg_color)
        style.configure("Office.TLabel", background=self._bg_color, foreground="#1f2933")
        style.configure("Header.TLabel", background=self._bg_color, foreground=self._accent_color, font=("Segoe UI Semibold", 16))
        style.configure("Status.TLabel", background=self._bg_color, foreground="#1f2933", font=("Segoe UI", 11))
        style.configure("Office.TButton", font=("Segoe UI", 10), padding=6)
        style.map(
            "Office.TButton",
            background=[("!disabled", "#ffffff"), ("active", "#274690"), ("pressed", "#1f3c88")],
            foreground=[("active", "#ffffff"), ("pressed", "#ffffff")],
        )
        style.configure("Danger.TButton", font=("Segoe UI", 10), padding=6)
        style.map(
            "Danger.TButton",
            background=[("!disabled", "#ffffff"), ("active", "#d0342c"), ("pressed", "#b91c1c")],
            foreground=[("active", "#ffffff"), ("pressed", "#ffffff")],
        )

    def _build_layout(self) -> None:
        main = ttk.Frame(self.root, style="Office.TFrame", padding=20)
        main.pack(fill="both", expand=True)

        header = ttk.Frame(main, style="Office.TFrame")
        header.pack(fill="x")
        ttk.Label(header, text="XML Watcher Control Center", style="Header.TLabel").pack(side="left")
        ttk.Label(header, textvariable=self.status_var, style="Status.TLabel").pack(side="right")

        controls = ttk.Frame(main, style="Office.TFrame")
        controls.pack(fill="x", pady=(15, 10))
        self.start_button = ttk.Button(controls, text="Start Watcher", style="Office.TButton", command=self.start_watcher)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="Stop Watcher", style="Danger.TButton", command=self.stop_watcher)
        self.stop_button.pack(side="left", padx=(12, 0))

        path_info = ttk.Frame(main, style="Office.TFrame")
        path_info.pack(fill="x")
        ttk.Label(path_info, textvariable=self.watch_info_var, style="Office.TLabel", wraplength=720).pack(anchor="w")
        ttk.Label(path_info, textvariable=self.ftp_info_var, style="Office.TLabel", wraplength=720).pack(anchor="w", pady=(2, 0))
        ttk.Label(path_info, textvariable=self.url_info_var, style="Office.TLabel", wraplength=720).pack(anchor="w", pady=(2, 0))

        ttk.Separator(main).pack(fill="x", pady=(18, 12))

        notebook = ttk.Notebook(main)
        notebook.pack(fill="both", expand=True)

        activity_frame = ttk.Frame(notebook, style="Office.TFrame", padding=(0, 10, 0, 0))
        notebook.add(activity_frame, text="Activity")

        ttk.Label(activity_frame, text="Activity Log", style="Office.TLabel").pack(anchor="w")

        log_frame = ttk.Frame(activity_frame, style="Office.TFrame")
        log_frame.pack(fill="both", expand=True, pady=(8, 0))
        self.log_display = ScrolledText(
            log_frame,
            wrap="word",
            font=("Consolas", 10),
            background="#ffffff",
            foreground="#1f2933",
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightcolor="#d4d4d8",
        )
        self.log_display.pack(fill="both", expand=True)
        self.log_display.bind("<Key>", lambda _: "break")

        settings_frame = ttk.Frame(notebook, style="Office.TFrame", padding=(0, 15, 0, 0))
        notebook.add(settings_frame, text="Settings")
        self._build_settings_tab(settings_frame)

    def _build_settings_tab(self, container: ttk.Frame) -> None:
        ttk.Label(
            container,
            text="Adjust watcher configuration and persist it for both the GUI and CLI tools.",
            style="Office.TLabel",
            wraplength=720,
        ).pack(anchor="w")

        form = ttk.Frame(container, style="Office.TFrame")
        form.pack(fill="x", pady=(12, 0))
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="Watch folder", style="Office.TLabel").grid(row=0, column=0, sticky="w", pady=4)
        watch_entry = ttk.Entry(form, textvariable=self.settings_watch_dir_var)
        watch_entry.grid(row=0, column=1, sticky="ew", padx=(10, 0))
        ttk.Button(form, text="Browse...", style="Office.TButton", command=self._browse_watch_dir).grid(
            row=0, column=2, sticky="ew", padx=(10, 0)
        )

        ttk.Label(form, text="Service URL", style="Office.TLabel").grid(row=1, column=0, sticky="w", pady=4)
        url_entry = ttk.Entry(form, textvariable=self.settings_url_var)
        url_entry.grid(row=1, column=1, sticky="ew", padx=(10, 0))

        ttk.Label(form, text="FTP base", style="Office.TLabel").grid(row=2, column=0, sticky="w", pady=4)
        ftp_combo = ttk.Combobox(
            form,
            textvariable=self.settings_ftp_base_var,
            values=("import", "export"),
            state="readonly",
        )
        ftp_combo.grid(row=2, column=1, sticky="w", padx=(10, 0))

        ttk.Label(
            container,
            text="Settings are saved to settings.json next to the executable. Updates restart the watcher if it is running.",
            style="Office.TLabel",
            wraplength=720,
        ).pack(anchor="w", pady=(16, 0))
        ttk.Label(
            container,
            text=f"Config file: {CONFIG_FILE}",
            style="Office.TLabel",
            wraplength=720,
        ).pack(anchor="w", pady=(2, 0))

        actions = ttk.Frame(container, style="Office.TFrame")
        actions.pack(fill="x", pady=(20, 0))
        ttk.Button(actions, text="Save Settings", style="Office.TButton", command=self.save_settings).pack(side="right")

    def _browse_watch_dir(self) -> None:
        initial = self.settings_watch_dir_var.get().strip()
        initial_dir = initial if initial and Path(initial).exists() else str(Path.home())
        selected = filedialog.askdirectory(parent=self.root, initialdir=initial_dir, title="Select Watch Folder")
        if selected:
            self.settings_watch_dir_var.set(selected)

    def _update_config_views(self, config: dict) -> None:
        self.watch_info_var.set(f"Watching directory: {config['watch_dir']}")
        self.url_info_var.set(f"POST endpoint: {config['url']}")
        ftp_base = config.get("ftp_base", "import")
        self.ftp_info_var.set(f"FTP root: /{ftp_base}")
        self.settings_watch_dir_var.set(config["watch_dir"])
        self.settings_url_var.set(config["url"])
        self.settings_ftp_base_var.set(ftp_base)

    def save_settings(self) -> None:
        watch_dir = self.settings_watch_dir_var.get().strip()
        url = self.settings_url_var.get().strip()
        ftp_base = self.settings_ftp_base_var.get().strip().lower()

        issues = []
        if not watch_dir:
            issues.append("- Watch folder is required.")
        if not url:
            issues.append("- Service URL is required.")
        if ftp_base not in ("import", "export"):
            issues.append("- FTP base must be either 'import' or 'export'.")
        if issues:
            messagebox.showerror("Settings", "\n".join(issues), parent=self.root)
            return

        watch_path = Path(watch_dir)
        if not watch_path.exists():
            confirm = messagebox.askyesno(
                "Confirm Watch Folder",
                "The selected folder does not exist. Continue and create it later?",
                parent=self.root,
            )
            if not confirm:
                return

        try:
            update_config(watch_dir=watch_dir, url=url, ftp_base=ftp_base)
        except Exception as exc:
            logger.exception("Failed to save configuration changes")
            messagebox.showerror("Settings", f"Failed to save settings:\n{exc}", parent=self.root)
            return

        runtime_config = reload_runtime_config()
        self._config = runtime_config
        was_running = self.service.is_running()
        restarted = self.service.set_watch_dir(runtime_config["watch_dir"], restart=was_running)
        self._update_config_views(runtime_config)

        if was_running and not restarted:
            messagebox.showwarning(
                "Watcher",
                "Watcher settings saved, but the watcher could not restart. Please verify the folder and start it manually.",
                parent=self.root,
            )
        else:
            messagebox.showinfo("Settings", "Watcher settings saved successfully.", parent=self.root)

        log("Watcher settings updated via GUI")

    def _resolve_formatter(self) -> logging.Formatter:
        for handler in logger.handlers:
            if handler.formatter:
                return handler.formatter
        return JakartaFormatter(fmt="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    def _load_existing_logs(self) -> None:
        try:
            if LOG_FILE.exists():
                existing = LOG_FILE.read_text(encoding="utf-8")
                if existing:
                    self.log_display.insert("end", existing.rstrip() + "\n")
                    self.log_display.see("end")
        except OSError as exc:
            self.log_display.insert("end", f"Unable to read existing logs: {exc}\n", "logWarning")

    def _auto_start_on_launch(self) -> None:
        if self.service.is_running():
            return
        log("Watcher auto-start requested on launch")
        self.start_watcher()

    def _apply_status(self) -> None:
        running = self.service.is_running()
        if running:
            self.status_var.set("Status: Running")
            self.start_button.state(["disabled"])
            self.stop_button.state(["!disabled"])
        else:
            self.status_var.set("Status: Stopped")
            self.start_button.state(["!disabled"])
            self.stop_button.state(["disabled"])

    def _schedule_status_refresh(self) -> None:
        self._apply_status()
        self._status_job = self.root.after(2000, self._schedule_status_refresh)

    def _on_unmap(self, _event=None) -> None:
        if self._closing:
            return
        # Only intercept actual minimization events
        if self.root.state() == "iconic":
            self._minimize_to_tray()

    def _on_map(self, _event=None) -> None:
        if self._closing:
            return
        # When returning to the foreground, ensure tray icon is cleaned up
        if self.root.state() == "normal":
            self._stop_tray_icon()

    def _minimize_to_tray(self) -> None:
        if self._closing:
            return
        if not self._tray_supported:
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                messagebox.showwarning(
                    "System Tray Unavailable",
                    "Install pystray and pillow to enable minimize-to-tray support.",
                    parent=self.root,
                )
            return
        if self._tray_icon is not None:
            return
        log("GUI minimizing to system tray")
        self.root.withdraw()
        self._ensure_tray_icon()

    def _ensure_tray_icon(self) -> None:
        if self._tray_icon is not None or not self._tray_supported:
            return
        image = self._create_tray_image()
        menu = pystray.Menu(
            pystray.MenuItem("Show Window", self._tray_on_restore),
            pystray.MenuItem("Start Watcher", self._tray_on_start),
            pystray.MenuItem("Stop Watcher", self._tray_on_stop),
            pystray.MenuItem("Exit", self._tray_on_exit),
        )
        icon = pystray.Icon("xml-watcher", image, "XML Watcher Control Center", menu)
        self._tray_icon = icon
        self._tray_thread = threading.Thread(target=icon.run, daemon=True)
        self._tray_thread.start()

    def _create_tray_image(self):
        if self._tray_image is not None:
            return self._tray_image
        accent = self._hex_to_rgba(self._accent_color)
        stroke = (255, 255, 255, 255)
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.ellipse((6, 6, 58, 58), fill=accent)
        draw.line((18, 20, 26, 44), fill=stroke, width=5)
        draw.line((26, 44, 32, 30), fill=stroke, width=5)
        draw.line((32, 30, 38, 44), fill=stroke, width=5)
        draw.line((38, 44, 46, 20), fill=stroke, width=5)
        self._tray_image = image
        return image

    def _hex_to_rgba(self, color: str, alpha: int = 255):
        color = color.lstrip("#")
        rgb = tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))
        return rgb + (alpha,)

    def _tray_on_restore(self, icon, _item) -> None:
        self.root.after(0, self._restore_from_tray)

    def _tray_on_exit(self, icon, _item) -> None:
        self.root.after(0, self._tray_exit_from_tray)

    def _tray_on_start(self, icon, _item) -> None:
        self.root.after(0, self.start_watcher)

    def _tray_on_stop(self, icon, _item) -> None:
        self.root.after(0, self.stop_watcher)

    def _restore_from_tray(self) -> None:
        self.root.deiconify()
        self.root.after(0, lambda: self.root.state("normal"))
        self.root.after(100, self.root.lift)
        log("GUI restored from system tray")
        self._stop_tray_icon()

    def _stop_tray_icon(self) -> None:
        icon = self._tray_icon
        if icon is None:
            return
        self._tray_icon = None
        try:
            icon.stop()
        except Exception:
            pass
        thread = self._tray_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1)
        self._tray_thread = None

    def _tray_exit_from_tray(self) -> None:
        log("GUI exit requested from system tray")
        self.on_close()

    def start_watcher(self) -> None:
        if self.service.is_running():
            self._apply_status()
            return
        runtime_config = reload_runtime_config()
        self._config = runtime_config
        self._update_config_views(runtime_config)
        if Path(runtime_config["watch_dir"]) != self.service.watch_dir:
            self.service.set_watch_dir(runtime_config["watch_dir"], restart=False)
        log("Start requested from GUI")
        try:
            started = self.service.start()
        except Exception as exc:
            logger.exception("Watcher failed to start from GUI")
            messagebox.showerror("Watcher Error", f"Failed to start watcher:\n{exc}", parent=self.root)
            return
        if not started:
            messagebox.showwarning(
                "Watcher",
                "Watcher did not start. Check that the watch directory exists and review the log.",
                parent=self.root,
            )
        self._apply_status()

    def stop_watcher(self) -> None:
        if not self.service.is_running():
            self._apply_status()
            return
        log("Stop requested from GUI")
        stopped = self.service.stop()
        if not stopped:
            messagebox.showwarning("Watcher", "Watcher was not running.", parent=self.root)
        self._apply_status()

    def on_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._stop_tray_icon()
        if self._status_job:
            self.root.after_cancel(self._status_job)
            self._status_job = None
        try:
            logger.removeHandler(self.text_handler)
            self.text_handler.close()
        finally:
            if self.service.is_running():
                self.service.stop()
            self.root.destroy()


def main() -> None:
    if not ensure_single_instance():
        return

    root = tk.Tk()
    try:
        WatcherApp(root)
        root.mainloop()
    finally:
        release_single_instance()


if __name__ == "__main__":
    main()
