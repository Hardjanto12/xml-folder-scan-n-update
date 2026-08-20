import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from pathlib import Path
import threading
import logging
from tkinter.scrolledtext import ScrolledText

# Import from existing modules
import xml.etree.ElementTree as ET
from convert_idr_xml import detect_and_read, strip_ns
import watch_and_post as backend
from config_manager import update_config

class BulkSenderApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("XML Bulk Sender")
        self.root.geometry("1000x700")
        self.root.minsize(800, 600)
        
        self.root.option_add("*Font", "{Segoe UI} 10")
        self._bg_color = "#f4f6fb"
        self._accent_color = "#1f3c88"
        self.root.configure(background=self._bg_color)
        
        backend.reload_runtime_config()
        self._config = backend.current_config()
        
        self.xml_files = [] 
        
        self._build_styles()
        self._build_layout()
        
        formatter = backend.JakartaFormatter(fmt="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        self.text_handler = TextHandler(self.log_display, formatter)
        backend.logger.addHandler(self.text_handler)
        
    def _build_styles(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Office.TFrame", background=self._bg_color)
        style.configure("Office.TLabel", background=self._bg_color, foreground="#1f2933")
        style.configure("Header.TLabel", background=self._bg_color, foreground=self._accent_color, font=("Segoe UI Semibold", 16))
        style.configure("Office.TButton", font=("Segoe UI", 10), padding=6)
        style.map("Office.TButton",
            background=[("!disabled", "#ffffff"), ("active", "#274690"), ("pressed", "#1f3c88")],
            foreground=[("active", "#ffffff"), ("pressed", "#ffffff")],
        )

    def _build_layout(self):
        main = ttk.Frame(self.root, style="Office.TFrame", padding=20)
        main.pack(fill="both", expand=True)

        header = ttk.Frame(main, style="Office.TFrame")
        header.pack(fill="x")
        ttk.Label(header, text="XML Bulk Sender", style="Header.TLabel").pack(side="left")
        
        self.watch_info_var = tk.StringVar(value=f"Folder: {self._config['watch_dir']}")
        self.url_info_var = tk.StringVar(value=f"Endpoint: {self._config['url']}")
        
        info_frame = ttk.Frame(main, style="Office.TFrame")
        info_frame.pack(fill="x", pady=5)
        ttk.Label(info_frame, textvariable=self.watch_info_var, style="Office.TLabel").pack(anchor="w")
        ttk.Label(info_frame, textvariable=self.url_info_var, style="Office.TLabel").pack(anchor="w")
        
        controls = ttk.Frame(main, style="Office.TFrame")
        controls.pack(fill="x", pady=(10, 5))
        ttk.Button(controls, text="Settings...", style="Office.TButton", command=self.open_settings).pack(side="left", padx=(0, 5))
        ttk.Button(controls, text="Scan Folder", style="Office.TButton", command=self.scan_folder).pack(side="left", padx=(0, 5))
        ttk.Button(controls, text="Select All", style="Office.TButton", command=self.select_all).pack(side="left", padx=(0, 5))
        ttk.Button(controls, text="Deselect All", style="Office.TButton", command=self.deselect_all).pack(side="left", padx=(0, 5))
        ttk.Button(controls, text="Send Selected", style="Office.TButton", command=self.send_selected).pack(side="right")
        
        table_frame = ttk.Frame(main)
        table_frame.pack(fill="both", expand=True, pady=5)
        
        columns = ("Selected", "No. Container", "Scan Time", "File Name", "Path", "Size", "Status")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings")
        self.tree.heading("Selected", text="[X]")
        self.tree.heading("No. Container", text="No. Container")
        self.tree.heading("Scan Time", text="Scan Time")
        self.tree.heading("File Name", text="File Name")
        self.tree.heading("Path", text="Path")
        self.tree.heading("Size", text="Size")
        self.tree.heading("Status", text="Status")
        
        self.tree.column("Selected", width=50, anchor="center")
        self.tree.column("No. Container", width=120)
        self.tree.column("Scan Time", width=120)
        self.tree.column("File Name", width=180)
        self.tree.column("Path", width=250)
        self.tree.column("Size", width=80, anchor="e")
        self.tree.column("Status", width=120)
        
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        
        self.tree.bind("<Button-1>", self.on_tree_press)
        self.tree.bind("<space>", self.on_space_pressed)
        
        log_frame = ttk.Frame(main, style="Office.TFrame")
        log_frame.pack(fill="both", expand=True, pady=(10, 0))
        ttk.Label(log_frame, text="Activity Log", style="Office.TLabel").pack(anchor="w")
        
        self.log_display = ScrolledText(
            log_frame, wrap="word", font=("Consolas", 10), height=8,
            background="#ffffff", foreground="#1f2933",
            relief="flat", borderwidth=0, highlightthickness=1, highlightcolor="#d4d4d8"
        )
        self.log_display.pack(fill="both", expand=True, pady=(5,0))
        self.log_display.bind("<Key>", lambda _: "break")

        # Schedule the scan after the GUI is fully rendered to avoid invisible hangs
        self.root.after(200, self.scan_folder)

    def on_tree_press(self, event):
        region = self.tree.identify("region", event.x, event.y)
        if region == "cell":
            column = self.tree.identify_column(event.x)
            item_id = self.tree.identify_row(event.y)
            if column == "#1" and item_id:
                idx = int(item_id)
                new_state = not self.xml_files[idx]['selected']
                
                selected_iids = self.tree.selection()
                if item_id in selected_iids and len(selected_iids) > 1:
                    for iid in selected_iids:
                        i = int(iid)
                        self.xml_files[i]['selected'] = new_state
                        self.refresh_tree_item(i)
                else:
                    self.xml_files[idx]['selected'] = new_state
                    self.refresh_tree_item(idx)
                return "break"

    def on_space_pressed(self, event):
        selected_iids = self.tree.selection()
        if not selected_iids:
            return
        first_idx = int(selected_iids[0])
        new_state = not self.xml_files[first_idx]['selected']
        for iid in selected_iids:
            i = int(iid)
            self.xml_files[i]['selected'] = new_state
            self.refresh_tree_item(i)
        return "break"

    def open_settings(self):
        top = tk.Toplevel(self.root)
        top.title("Settings")
        top.geometry("500x250")
        
        ttk.Label(top, text="Watch folder:").pack(anchor="w", padx=10, pady=5)
        
        watch_frame = ttk.Frame(top)
        watch_frame.pack(fill="x", padx=10)
        watch_var = tk.StringVar(value=self._config["watch_dir"])
        ttk.Entry(watch_frame, textvariable=watch_var).pack(side="left", fill="x", expand=True)
        
        def browse():
            folder = filedialog.askdirectory(initialdir=watch_var.get(), parent=top)
            if folder:
                watch_var.set(folder)
        
        ttk.Button(watch_frame, text="Browse", command=browse).pack(side="right", padx=(5,0))
        
        ttk.Label(top, text="Service URL:").pack(anchor="w", padx=10, pady=5)
        url_var = tk.StringVar(value=self._config["url"])
        ttk.Entry(top, textvariable=url_var).pack(fill="x", padx=10)
        
        ttk.Label(top, text="FTP Base:").pack(anchor="w", padx=10, pady=5)
        ftp_var = tk.StringVar(value=self._config.get("ftp_base", "import"))
        ttk.Combobox(top, textvariable=ftp_var, values=("import", "export"), state="readonly").pack(fill="x", padx=10)
        
        def save():
            update_config(watch_dir=watch_var.get(), url=url_var.get(), ftp_base=ftp_var.get())
            backend.reload_runtime_config()
            self._config = backend.current_config()
            self.watch_info_var.set(f"Folder: {self._config['watch_dir']}")
            self.url_info_var.set(f"Endpoint: {self._config['url']}")
            top.destroy()
            self.scan_folder()
            
        ttk.Button(top, text="Save Settings", command=save).pack(pady=15)
        
    def scan_folder(self):
        watch_dir = Path(self._config['watch_dir'])
        if not watch_dir.exists():
            messagebox.showerror("Error", f"Folder does not exist:\n{watch_dir}")
            return
            
        def do_scan():
            temp_files = []
            i = 0
            for child in watch_dir.rglob('*.xml'):
                if child.is_file():
                    scan_time = ""
                    container_no = ""
                    try:
                        xml_text = detect_and_read(child)
                        root = ET.fromstring(strip_ns(xml_text))
                        idr_img = root.find('./IDR_IMAGE')
                        if idr_img is not None:
                            picno = (idr_img.findtext('PICNO') or '').strip()
                            scan_time = (idr_img.findtext('SCANTIME') or '').strip()
                            
                            container_elem = root.find('.//IDR_SII_INPUTINFO_CONTAINER/CONTAINER_NO')
                            if container_elem is not None and (container_elem.text or '').strip():
                                container_no = (container_elem.text or '').strip()
                            else:
                                container_no = picno
                    except Exception as e:
                        backend.log(f"Failed to parse metadata for {child.name}: {e}", logging.DEBUG)
                    
                    temp_files.append({
                        'id': i,
                        'path': child,
                        'selected': True,
                        'status': 'Pending',
                        'container_no': container_no,
                        'scan_time': scan_time
                    })
                    i += 1
                    
            # Safely update UI from the main thread
            self.root.after(0, self._apply_scan_results, temp_files, watch_dir)
            
        # Clear existing items and show scanning status
        for item in self.tree.get_children():
            self.tree.delete(item)
        backend.log(f"Scanning folder {watch_dir} ...")
        threading.Thread(target=do_scan, daemon=True).start()

    def _apply_scan_results(self, files, watch_dir):
        self.xml_files = files
        self.refresh_tree()
        backend.log(f"Scanned {len(self.xml_files)} XML files in {watch_dir}")

    def select_all(self):
        for f in self.xml_files: f['selected'] = True
        self.refresh_tree()

    def deselect_all(self):
        for f in self.xml_files: f['selected'] = False
        self.refresh_tree()

    def refresh_tree(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        for i, f in enumerate(self.xml_files):
            size_kb = f['path'].stat().st_size / 1024
            sel_text = "[X]" if f['selected'] else "[ ]"
            rel_path = f['path'].relative_to(Path(self._config['watch_dir']))
            self.tree.insert("", "end", iid=str(i), values=(
                sel_text, f.get('container_no', ''), f.get('scan_time', ''),
                f['path'].name, str(rel_path.parent), f"{size_kb:.1f} KB", f['status']
            ))

    def refresh_tree_item(self, idx):
        f = self.xml_files[idx]
        size_kb = f['path'].stat().st_size / 1024
        sel_text = "[X]" if f['selected'] else "[ ]"
        try:
            rel_path = f['path'].relative_to(Path(self._config['watch_dir']))
            parent_str = str(rel_path.parent)
        except ValueError:
            parent_str = str(f['path'].parent)
        self.tree.item(str(idx), values=(
            sel_text, f.get('container_no', ''), f.get('scan_time', ''),
            f['path'].name, parent_str, f"{size_kb:.1f} KB", f['status']
        ))

    def send_selected(self):
        selected_files = [(i, f) for i, f in enumerate(self.xml_files) if f['selected'] and f['status'] != 'Success']
        if not selected_files:
            messagebox.showinfo("Info", "No pending files selected.")
            return
            
        def worker():
            for idx, f in selected_files:
                path = f['path']
                f['status'] = 'Processing...'
                self.root.after(0, self.refresh_tree_item, idx)
                
                try:
                    payload = backend.convert_xml_to_payload(path)
                    resp = backend.send_payload(payload)
                    if 200 <= resp.status_code < 300:
                        f['status'] = 'Success'
                        backend.log(f"Successfully sent: {path.name}")
                    else:
                        f['status'] = f'Error {resp.status_code}'
                        backend.log(f"Error {resp.status_code} sending {path.name}", logging.WARNING)
                except Exception as e:
                    f['status'] = 'Failed'
                    backend.log(f"Failed to send {path.name}: {e}", logging.ERROR)
                    
                self.root.after(0, self.refresh_tree_item, idx)
            
            self.root.after(0, lambda: messagebox.showinfo("Done", "Bulk send completed."))
            
        threading.Thread(target=worker, daemon=True).start()

class TextHandler(logging.Handler):
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

if __name__ == "__main__":
    root = tk.Tk()
    app = BulkSenderApp(root)
    root.mainloop()
