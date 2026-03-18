import io
import json
import os
import threading
import time
import tkinter as tk
from datetime import datetime
from queue import Empty, Queue
from tkinter import filedialog, ttk
from urllib.parse import urlparse

import imageio.v2 as imageio
import pystray
import requests
from PIL import Image
from pynput import keyboard as pynput_keyboard
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

SETTINGS_FILE = "settings.json"
LOGO_FILE = "logo.png"
ALLOWED_WEBHOOK_HOSTS = {
    "discord.com",
    "ptb.discord.com",
    "canary.discord.com",
    "discordapp.com",
}
SIZE_LIMITS = {
    "catbox": 200 * 1024 * 1024,
    "buzzheavier": 1024 * 1024 * 1024,
    "gofile": 5 * 1024 * 1024 * 1024,
    "litterbox": float("inf"),
}
DEFAULT_SETTINGS = {
    "watch_path": os.path.expanduser("~/Videos"),
    "webhook_url": "",
    "overlay_mode": True,
    "auto_upload": True,
    "keybind": "F8",
}


class WatchHandler(FileSystemEventHandler):
    def __init__(self, engine):
        super().__init__()
        self.engine = engine

    def on_created(self, event):
        if event.is_directory:
            return
        self.engine.handle_created_file(event.src_path)


class UploaderEngine:
    def __init__(self, log_callback, status_callback):
        self.log_callback = log_callback
        self.status_callback = status_callback
        self.watch_path = DEFAULT_SETTINGS["watch_path"]
        self.webhook_url = ""
        self.auto_upload = True
        self.processed_files = set()
        self.observer = None
        self.lock = threading.Lock()
        self.current_status = "idle"
        self.current_file = ""

    def configure(self, watch_path, webhook_url, auto_upload):
        self.watch_path = os.path.abspath(watch_path)
        self.webhook_url = webhook_url
        self.auto_upload = auto_upload

    def start(self):
        if not self.auto_upload:
            self.set_status("idle", "")
            self.stop_observer()
            return
        self.stop_observer()
        os.makedirs(self.watch_path, exist_ok=True)
        self.observer = Observer()
        self.observer.schedule(WatchHandler(self), self.watch_path, recursive=True)
        self.observer.start()
        self.log(f"Watching: {self.watch_path}")

    def stop(self):
        self.stop_observer()
        self.set_status("idle", "")

    def stop_observer(self):
        if self.observer:
            self.observer.stop()
            self.observer.join(timeout=2)
            self.observer = None

    def handle_created_file(self, file_path):
        if not self.is_safe_upload_path(file_path):
            self.log(f"Blocked unsafe path: {os.path.basename(file_path)}")
            return
        if not self.wait_for_file_ready(file_path):
            self.log(f"Skipping file not ready: {os.path.basename(file_path)}")
            return
        with self.lock:
            if file_path in self.processed_files:
                return
            self.processed_files.add(file_path)
        threading.Thread(target=self.process_file, args=(file_path,), daemon=True).start()

    def upload_latest_now(self):
        threading.Thread(target=self._upload_latest_worker, daemon=True).start()

    def _upload_latest_worker(self):
        latest_file = self.find_latest_file(self.watch_path)
        if not latest_file:
            self.log("No file found in selected path")
            return
        if not self.is_safe_upload_path(latest_file):
            self.log(f"Blocked unsafe latest file: {os.path.basename(latest_file)}")
            return
        if not self.wait_for_file_ready(latest_file):
            self.log(f"Latest file is still being written: {os.path.basename(latest_file)}")
            return
        self.log(f"Manual upload triggered for: {os.path.basename(latest_file)}")
        self.process_file(latest_file)

    def normalize_path(self, path):
        return os.path.normcase(os.path.realpath(os.path.abspath(path)))

    def is_safe_upload_path(self, file_path):
        try:
            if os.path.islink(file_path):
                return False
            watched_root = self.normalize_path(self.watch_path)
            target_path = self.normalize_path(file_path)
            if target_path == watched_root:
                return False
            return target_path.startswith(watched_root + os.sep)
        except Exception:
            return False

    def find_latest_file(self, root_path):
        newest_path = None
        newest_stamp = -1
        skip_suffixes = (
            ".tmp",
            ".part",
            ".crdownload",
            ".download",
            ".partial",
            ".opdownload",
            ".!qB",
        )
        for root, _, files in os.walk(root_path):
            for name in files:
                file_path = os.path.join(root, name)
                low = name.lower()
                if low.endswith(skip_suffixes):
                    continue
                try:
                    stats = os.stat(file_path)
                except OSError:
                    continue
                if os.path.islink(file_path):
                    continue
                stamp = max(getattr(stats, "st_mtime_ns", int(stats.st_mtime * 1_000_000_000)), getattr(stats, "st_ctime_ns", int(stats.st_ctime * 1_000_000_000)))
                if stamp > newest_stamp:
                    newest_stamp = stamp
                    newest_path = file_path
        return newest_path

    def process_file(self, file_path):
        try:
            if not os.path.exists(file_path):
                return
            file_size = os.path.getsize(file_path)
            file_name = os.path.basename(file_path)
            self.set_status("uploading", file_name)

            if file_size > SIZE_LIMITS["catbox"]:
                uploads = []
                for svc_name, svc_func in (("gofile", self.upload_to_gofile), ("litterbox", self.upload_to_litterbox)):
                    self.log(f"Trying {svc_name} ({file_size / 1024 / 1024:.2f}MB)")
                    url = svc_func(file_path)
                    if url:
                        uploads.append((svc_name, url))

                if uploads:
                    self.send_webhook(file_path, uploads, file_size)
                    services = ", ".join([name for name, _ in uploads])
                    self.log(f"Uploaded {file_name} via {services}")
                    self.set_status("success", file_name)
                else:
                    self.log(f"Failed {file_name}: gofile and litterbox both failed")
                    self.set_status("failed", file_name)
                self.reset_status_later()
                return

            upload_order = [
                ("catbox", self.upload_to_catbox),
                ("buzzheavier", self.upload_to_buzzheavier),
                ("gofile", self.upload_to_gofile),
                ("litterbox", self.upload_to_litterbox),
            ]

            if file_size > SIZE_LIMITS["catbox"] and file_size <= SIZE_LIMITS["buzzheavier"]:
                upload_order = [
                    ("buzzheavier", self.upload_to_buzzheavier),
                    ("gofile", self.upload_to_gofile),
                    ("litterbox", self.upload_to_litterbox),
                    ("catbox", self.upload_to_catbox),
                ]

            success = None
            for svc_name, svc_func in upload_order:
                self.log(f"Trying {svc_name} ({file_size / 1024 / 1024:.2f}MB)")
                url = svc_func(file_path)
                if url:
                    success = (svc_name, url)
                    break

            if success:
                self.send_webhook(file_path, [success], file_size)
                self.log(f"Uploaded {file_name} via {success[0]}")
                self.set_status("success", file_name)
            else:
                self.log(f"Failed {file_name}: all services failed")
                self.set_status("failed", file_name)

        except Exception as exc:
            self.log(f"Error processing {os.path.basename(file_path)}: {exc}")
            self.set_status("error", os.path.basename(file_path))
        finally:
            self.reset_status_later()

    def reset_status_later(self):
        def reset_task():
            time.sleep(3)
            self.set_status("idle", "")

        threading.Thread(target=reset_task, daemon=True).start()

    def set_status(self, status, current_file):
        self.current_status = status
        self.current_file = current_file
        self.status_callback(status, current_file)

    def wait_for_file_ready(self, file_path, attempts=20, delay=0.5):
        last_size = -1
        stable_count = 0
        for _ in range(attempts):
            if not os.path.exists(file_path):
                time.sleep(delay)
                continue
            try:
                size = os.path.getsize(file_path)
            except OSError:
                time.sleep(delay)
                continue
            if size > 0 and size == last_size:
                stable_count += 1
                if stable_count >= 2:
                    return True
            else:
                stable_count = 0
                last_size = size
            time.sleep(delay)
        return os.path.exists(file_path) and os.path.getsize(file_path) > 0

    def upload_to_catbox(self, file_path):
        try:
            if os.path.getsize(file_path) > SIZE_LIMITS["catbox"]:
                return None
            with open(file_path, "rb") as f:
                files = {"fileToUpload": (os.path.basename(file_path), f)}
                headers = {"User-Agent": "Mozilla/5.0"}
                response = requests.post(
                    "https://catbox.moe/user/api.php",
                    files=files,
                    data={"reqtype": "fileupload"},
                    headers=headers,
                    timeout=120,
                )
                text = response.text.strip()
                if response.status_code == 200 and text.startswith("http") and "error" not in text.lower():
                    return text
                self.log(f"Catbox {response.status_code}: {text[:160]}")
        except Exception as exc:
            self.log(f"Catbox error: {exc}")
        return None

    def upload_to_buzzheavier(self, file_path):
        try:
            if os.path.getsize(file_path) > 20 * 1024 * 1024:
                return None
            with open(file_path, "rb") as f:
                files = {"file": (os.path.basename(file_path), f)}
                response = requests.post("https://buzzheavier.com/api/upload", files=files, timeout=120)
                if response.status_code == 200:
                    data = response.json()
                    if "file" in data and "link" in data["file"]:
                        return data["file"]["link"]
                    if "files" in data and len(data["files"]) > 0:
                        first = data["files"][0]
                        if "link" in first:
                            return first["link"]
                        if "id" in first:
                            return f"https://buzzheavier.com/{first['id']}"
                elif response.status_code == 413:
                    self.log("Buzzheavier rejected by size limit")
                else:
                    self.log(f"Buzzheavier {response.status_code}: {response.text[:160]}")
        except Exception as exc:
            self.log(f"Buzzheavier error: {exc}")
        return None

    def upload_to_gofile(self, file_path):
        try:
            if os.path.getsize(file_path) > SIZE_LIMITS["gofile"]:
                return None
            with open(file_path, "rb") as f:
                files = {"file": (os.path.basename(file_path), f, "application/octet-stream")}
                response = requests.post("https://upload.gofile.io/uploadfile", files=files, timeout=180)
                try:
                    result = response.json()
                except json.JSONDecodeError:
                    self.log(f"Gofile non-JSON {response.status_code}: {response.text[:160]}")
                    return None
                if result.get("status") == "ok" and isinstance(result.get("data"), dict):
                    data = result["data"]
                    if data.get("downloadPage"):
                        return data["downloadPage"]
                    if data.get("code"):
                        return f"https://gofile.io/d/{data['code']}"
                self.log(f"Gofile response: {str(result)[:200]}")
        except Exception as exc:
            self.log(f"Gofile error: {exc}")
        return None

    def upload_to_litterbox(self, file_path):
        try:
            with open(file_path, "rb") as f:
                files = {"fileToUpload": (os.path.basename(file_path), f)}
                response = requests.post(
                    "https://litterbox.catbox.moe/resources/internals/api.php",
                    files=files,
                    data={"reqtype": "fileupload", "time": "24h"},
                    timeout=180,
                )
                text = response.text.strip()
                if response.status_code == 200 and text.startswith("http"):
                    return text
                self.log(f"Litterbox {response.status_code}: {text[:160]}")
        except Exception as exc:
            self.log(f"Litterbox error: {exc}")
        return None

    def build_preview_image_bytes(self, file_path):
        ext = os.path.splitext(file_path)[1].lower()
        image_exts = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
        video_exts = {".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v"}
        try:
            if os.path.getsize(file_path) > 1024 * 1024 * 1024:
                return None
            if ext in image_exts:
                with Image.open(file_path) as img:
                    frame = img.convert("RGB")
                    frame.thumbnail((1280, 720))
                    out = io.BytesIO()
                    frame.save(out, format="JPEG", quality=90)
                    out.seek(0)
                    return out.read()
            if ext in video_exts:
                reader = imageio.get_reader(file_path)
                frame_data = reader.get_data(0)
                reader.close()
                frame = Image.fromarray(frame_data).convert("RGB")
                frame.thumbnail((1280, 720))
                out = io.BytesIO()
                frame.save(out, format="JPEG", quality=90)
                out.seek(0)
                return out.read()
        except Exception as exc:
            self.log(f"Preview failed: {exc}")
        return None

    def send_webhook(self, file_path, uploads, file_size):
        if not self.webhook_url.strip():
            self.log("Webhook URL is empty")
            return
        if not is_valid_discord_webhook(self.webhook_url):
            self.log("Blocked webhook: only valid Discord webhook URLs are allowed")
            return
        try:
            file_name = os.path.basename(file_path)
            file_size_mb = file_size / (1024 * 1024)
            services_text = ", ".join([name.upper() for name, _ in uploads])
            primary_url = uploads[0][1]
            links_block = "\n".join([f"{name.upper()}: {url}" for name, url in uploads])
            embed = {
                "title": file_name,
                "description": f"Size: {file_size_mb:.2f} MB\nService: {services_text}\n{links_block}",
                "url": primary_url,
                "color": 3447003,
                "timestamp": datetime.utcnow().isoformat(),
            }
            preview_bytes = self.build_preview_image_bytes(file_path)
            if preview_bytes:
                embed["image"] = {"url": "attachment://preview.jpg"}
                payload = {"embeds": [embed]}
                files = {"file": ("preview.jpg", preview_bytes, "image/jpeg")}
                response = requests.post(
                    self.webhook_url,
                    data={"payload_json": json.dumps(payload)},
                    files=files,
                    timeout=20,
                    allow_redirects=False,
                )
            else:
                payload = {"embeds": [embed]}
                response = requests.post(self.webhook_url, json=payload, timeout=10, allow_redirects=False)
            response.raise_for_status()
        except Exception as exc:
            self.log(f"Webhook error: {exc}")

    def log(self, message):
        self.log_callback(message)


class OverlayWindow:
    def __init__(self, root):
        self.root = root
        self.window = None
        self.status_var = tk.StringVar(value="IDLE")
        self.file_var = tk.StringVar(value="")

    def show(self):
        if self.window and self.window.winfo_exists():
            return
        self.window = tk.Toplevel(self.root)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.configure(bg="#0d1224")
        frame = tk.Frame(self.window, bg="#0d1224", bd=1, relief="solid")
        frame.pack(fill="both", expand=True)
        title = tk.Label(frame, text="Uploader Overlay", fg="#7dd3fc", bg="#0d1224", font=("Segoe UI", 9, "bold"))
        title.pack(anchor="w", padx=8, pady=(6, 2))
        status = tk.Label(frame, textvariable=self.status_var, fg="#e2e8f0", bg="#0d1224", font=("Segoe UI", 10, "bold"))
        status.pack(anchor="w", padx=8)
        file_label = tk.Label(frame, textvariable=self.file_var, fg="#94a3b8", bg="#0d1224", font=("Segoe UI", 9), wraplength=260, justify="left")
        file_label.pack(anchor="w", padx=8, pady=(2, 8))
        self.window.geometry("280x90+20+20")

    def hide(self):
        if self.window and self.window.winfo_exists():
            self.window.destroy()
            self.window = None

    def update(self, status, file_name):
        self.status_var.set(status.upper())
        self.file_var.set(file_name)


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("disclipy")
        self.root.geometry("920x620")
        self.root.minsize(860, 560)
        self.log_queue = Queue()
        self.settings = self.load_settings()
        self.overlay = OverlayWindow(root)
        self.current_binding = None
        self.global_hotkey_listener = None
        self.tray_icon = None
        self.window_icon = None

        self.watch_path_var = tk.StringVar(value=self.settings["watch_path"])
        self.webhook_var = tk.StringVar(value=self.settings["webhook_url"])
        self.overlay_mode_var = tk.BooleanVar(value=self.settings["overlay_mode"])
        self.auto_upload_var = tk.BooleanVar(value=self.settings["auto_upload"])
        self.keybind_var = tk.StringVar(value=self.settings["keybind"])
        self.status_var = tk.StringVar(value="IDLE")
        self.file_var = tk.StringVar(value="")

        self.engine = UploaderEngine(self.push_log, self.on_engine_status)
        self.apply_window_icon()
        self.build_ui()
        self.setup_keybind()
        self.apply_overlay_state()
        self.setup_tray()
        self.poll_logs()
        self.root.protocol("WM_DELETE_WINDOW", self.on_window_close)

    def apply_window_icon(self):
        logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), LOGO_FILE)
        if not os.path.exists(logo_path):
            return
        try:
            self.window_icon = tk.PhotoImage(file=logo_path)
            self.root.iconphoto(True, self.window_icon)
        except Exception:
            self.window_icon = None

    def load_settings(self):
        if not os.path.exists(SETTINGS_FILE):
            return DEFAULT_SETTINGS.copy()
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            out = DEFAULT_SETTINGS.copy()
            out.update(data)
            return out
        except Exception:
            return DEFAULT_SETTINGS.copy()

    def save_settings(self):
        data = {
            "watch_path": self.watch_path_var.get().strip(),
            "webhook_url": self.webhook_var.get().strip(),
            "overlay_mode": self.overlay_mode_var.get(),
            "auto_upload": self.auto_upload_var.get(),
            "keybind": self.keybind_var.get().strip() or "F8",
        }
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def build_ui(self):
        style = ttk.Style()
        style.theme_use("clam")
        self.root.configure(bg="#dfe6ed")
        style.configure("Root.TFrame", background="#dfe6ed")
        style.configure("Surface.TFrame", background="#edf1f6")
        style.configure("Card.TFrame", background="#f6f8fb")
        style.configure("Card.TLabel", background="#f6f8fb", foreground="#31353b", font=("Segoe UI", 10))
        style.configure("Title.TLabel", background="#edf1f6", foreground="#22252a", font=("Segoe UI Semibold", 22))
        style.configure("Section.TLabel", background="#f6f8fb", foreground="#3d434b", font=("Segoe UI", 9, "bold"))
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"), padding=(10, 8))
        style.configure("Plain.TButton", font=("Segoe UI", 10), padding=(10, 8))
        style.configure("Run.TButton", font=("Segoe UI", 10, "bold"), padding=(10, 8))
        style.configure("Stop.TButton", font=("Segoe UI", 10, "bold"), padding=(10, 8))
        style.map("Run.TButton", background=[("!disabled", "#ddf7ef")], foreground=[("!disabled", "#238c6b")])
        style.map("Stop.TButton", background=[("!disabled", "#faeded")], foreground=[("!disabled", "#a45151")])
        style.map("Accent.TButton", background=[("!disabled", "#f3f4f6")], foreground=[("!disabled", "#2f343a")])
        style.configure("Mode.TRadiobutton", background="#f6f8fb", foreground="#2f343a", font=("Segoe UI", 10))
        style.configure("Mode.TCheckbutton", background="#f6f8fb", foreground="#2f343a", font=("Segoe UI", 10))
        style.configure("Main.TEntry", fieldbackground="#ffffff", foreground="#1f2937", bordercolor="#d7dce4", lightcolor="#d7dce4", darkcolor="#d7dce4", padding=7)

        root_frame = ttk.Frame(self.root, style="Root.TFrame")
        root_frame.pack(fill="both", expand=True)

        shell = ttk.Frame(root_frame, style="Surface.TFrame", padding=18)
        shell.pack(fill="both", expand=True, padx=18, pady=18)

        header = ttk.Label(shell, text="disclipy", style="Title.TLabel")
        header.pack(anchor="w", padx=14, pady=(8, 14))

        panel = ttk.Frame(shell, style="Card.TFrame", padding=16)
        panel.pack(fill="x", padx=10, pady=(0, 12))

        ttk.Label(panel, text="WATCH PATH", style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 4))
        path_row = ttk.Frame(panel, style="Card.TFrame")
        path_row.grid(row=1, column=0, columnspan=2, sticky="we", pady=(0, 10))
        ttk.Entry(path_row, textvariable=self.watch_path_var, width=78, style="Main.TEntry").pack(side="left", fill="x", expand=True)
        ttk.Button(path_row, text="Browse...", command=self.browse_folder, style="Plain.TButton").pack(side="left", padx=(8, 0))

        ttk.Label(panel, text="WEBHOOK URL", style="Section.TLabel").grid(row=2, column=0, sticky="w", pady=(0, 4))
        ttk.Entry(panel, textvariable=self.webhook_var, width=78, style="Main.TEntry").grid(row=3, column=0, columnspan=2, pady=(0, 12), sticky="we")
        panel.columnconfigure(0, weight=1)

        mode_row = ttk.Frame(panel, style="Card.TFrame")
        mode_row.grid(row=4, column=0, columnspan=2, sticky="we", pady=(0, 8))

        ttk.Radiobutton(mode_row, text="AUTO UPLOAD", variable=self.auto_upload_var, value=True, style="Mode.TRadiobutton").pack(side="left", padx=(0, 12))
        ttk.Radiobutton(mode_row, text="MANUAL WITH KEYBIND", variable=self.auto_upload_var, value=False, style="Mode.TRadiobutton").pack(side="left", padx=(0, 12))

        keybind_entry = ttk.Entry(mode_row, textvariable=self.keybind_var, width=16, style="Main.TEntry")
        keybind_entry.pack(side="left")
        keybind_entry.bind("<FocusOut>", lambda _: self.setup_keybind())

        ttk.Checkbutton(mode_row, text="OVERLAY MODE", variable=self.overlay_mode_var, command=self.apply_overlay_state, style="Mode.TCheckbutton").pack(side="right", padx=(12, 0))

        action_row = ttk.Frame(panel, style="Card.TFrame")
        action_row.grid(row=5, column=0, columnspan=2, sticky="we", pady=(2, 0))

        ttk.Button(action_row, text="Save Settings", command=self.on_save, style="Accent.TButton").pack(side="left", padx=(0, 8))
        ttk.Button(action_row, text="Start", command=self.on_start, style="Run.TButton").pack(side="left", padx=(0, 8))
        ttk.Button(action_row, text="Stop", command=self.on_stop, style="Stop.TButton").pack(side="left", padx=(0, 8))
        ttk.Button(action_row, text="Upload Latest Now", command=self.on_upload_latest, style="Plain.TButton").pack(side="left")

        status_panel = ttk.Frame(shell, style="Card.TFrame", padding=16)
        status_panel.pack(fill="both", expand=True, padx=10)

        status_row = tk.Frame(status_panel, bg="#f6f8fb")
        status_row.pack(fill="x", pady=(0, 4))
        status_title = tk.Label(status_row, text="STATUS:", bg="#f6f8fb", fg="#30343a", font=("Segoe UI", 19, "bold"))
        status_title.pack(side="left")
        self.status_text_label = tk.Label(status_row, textvariable=self.status_var, bg="#f6f8fb", fg="#29a062", font=("Segoe UI", 19, "bold"))
        self.status_text_label.pack(side="left", padx=(10, 8))
        self.status_dot_label = tk.Label(status_row, text="●", bg="#f6f8fb", fg="#29a062", font=("Segoe UI", 17))
        self.status_dot_label.pack(side="left")

        current_row = tk.Frame(status_panel, bg="#f6f8fb")
        current_row.pack(fill="x", pady=(2, 8))
        tk.Label(current_row, text="Current File:", bg="#f6f8fb", fg="#41464d", font=("Segoe UI", 11)).pack(anchor="w")
        tk.Label(current_row, textvariable=self.file_var, bg="#f6f8fb", fg="#2f343a", font=("Segoe UI", 11)).pack(anchor="w")

        log_panel = ttk.Frame(status_panel, style="Card.TFrame", padding=8)
        log_panel.pack(fill="both", expand=True, pady=(0, 0))
        ttk.Label(log_panel, text="ACTIVITY LOG", style="Section.TLabel").pack(anchor="w", pady=(0, 6))

        self.log_text = tk.Text(
            log_panel,
            bg="#ffffff",
            fg="#2c333a",
            insertbackground="#2c333a",
            relief="solid",
            borderwidth=1,
            font=("Consolas", 10),
            wrap="word",
            height=14,
        )
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")

    def browse_folder(self):
        selected = filedialog.askdirectory(initialdir=self.watch_path_var.get() or os.path.expanduser("~"))
        if selected:
            self.watch_path_var.set(selected)

    def setup_keybind(self):
        hotkey_expr = self.parse_global_hotkey(self.keybind_var.get().strip() or "F8")
        if self.global_hotkey_listener:
            self.global_hotkey_listener.stop()
            self.global_hotkey_listener = None
        try:
            self.global_hotkey_listener = pynput_keyboard.GlobalHotKeys({hotkey_expr: self.on_global_hotkey})
            self.global_hotkey_listener.start()
            self.current_binding = hotkey_expr
            self.push_log(f"Global keybind active: {self.keybind_var.get().strip() or 'F8'}")
        except Exception as exc:
            self.push_log(f"Keybind error: {exc}")

    def parse_global_hotkey(self, keybind):
        clean = keybind.replace(" ", "")
        parts = clean.split("+")
        if not parts:
            return "<f8>"
        modifier_map = {
            "ctrl": "<ctrl>",
            "control": "<ctrl>",
            "alt": "<alt>",
            "shift": "<shift>",
            "win": "<cmd>",
            "windows": "<cmd>",
            "cmd": "<cmd>",
        }
        mapped = []
        for index, part in enumerate(parts):
            low = part.lower()
            if low in modifier_map:
                mapped.append(modifier_map[low])
            else:
                if low.startswith("f") and low[1:].isdigit():
                    mapped.append(f"<{low}>")
                elif len(low) == 1:
                    mapped.append(low)
                elif index == len(parts) - 1:
                    mapped.append(low)
        if not mapped:
            return "<f8>"
        return "+".join(mapped)

    def on_global_hotkey(self):
        self.root.after(0, self.on_hotkey)

    def on_hotkey(self, _event=None):
        if self.auto_upload_var.get():
            return
        self.on_upload_latest()

    def on_save(self):
        self.save_settings()
        self.push_log("Settings saved")
        self.apply_overlay_state()
        self.setup_keybind()

    def on_start(self):
        self.save_settings()
        path = self.watch_path_var.get().strip()
        webhook = self.webhook_var.get().strip()
        if not path:
            self.push_log("Path is required")
            return
        self.engine.configure(path, webhook, self.auto_upload_var.get())
        self.engine.start()
        mode_text = "Auto Upload" if self.auto_upload_var.get() else "Manual Keybind"
        self.push_log(f"Started in {mode_text} mode")

    def on_stop(self):
        self.engine.stop()
        self.push_log("Stopped")

    def on_upload_latest(self):
        self.save_settings()
        path = self.watch_path_var.get().strip()
        webhook = self.webhook_var.get().strip()
        if not path:
            self.push_log("Path is required")
            return
        self.engine.configure(path, webhook, self.auto_upload_var.get())
        self.engine.upload_latest_now()

    def on_engine_status(self, status, file_name):
        self.status_var.set(status.upper())
        self.file_var.set(file_name)
        self.overlay.update(status, file_name)
        color = "#29a062"
        if status in ("failed", "error"):
            color = "#cc4d4d"
        elif status == "uploading":
            color = "#c68a17"
        elif status == "idle":
            color = "#29a062"
        self.status_text_label.configure(fg=color)
        self.status_dot_label.configure(fg=color)

    def apply_overlay_state(self):
        if self.overlay_mode_var.get():
            self.overlay.show()
        else:
            self.overlay.hide()

    def push_log(self, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_queue.put(f"[{timestamp}] - {message}")

    def poll_logs(self):
        try:
            while True:
                line = self.log_queue.get_nowait()
                self.log_text.configure(state="normal")
                self.log_text.insert("end", line + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except Empty:
            pass
        self.root.after(150, self.poll_logs)

    def build_tray_icon(self):
        logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), LOGO_FILE)
        if os.path.exists(logo_path):
            try:
                image = Image.open(logo_path).convert("RGBA")
                return image.resize((64, 64), Image.LANCZOS)
            except Exception:
                pass
        return None

    def setup_tray(self):
        icon_image = self.build_tray_icon()
        if icon_image is None:
            self.push_log("Tray icon not started: logo.png missing or invalid")
            return

        def show_window(_icon, _item):
            self.root.after(0, self.show_window)

        def hide_window(_icon, _item):
            self.root.after(0, self.hide_window)

        def upload_latest(_icon, _item):
            self.root.after(0, self.on_upload_latest)

        def quit_app(_icon, _item):
            self.root.after(0, self.on_quit)

        menu = pystray.Menu(
            pystray.MenuItem("Show", show_window),
            pystray.MenuItem("Hide", hide_window),
            pystray.MenuItem("Upload Latest", upload_latest),
            pystray.MenuItem("Quit", quit_app),
        )
        self.tray_icon = pystray.Icon("disclipy", icon_image, "disclipy", menu)
        self.tray_icon.run_detached()

    def show_window(self):
        self.root.deiconify()
        self.root.lift()

    def hide_window(self):
        self.root.withdraw()

    def on_window_close(self):
        self.hide_window()
        self.push_log("Window hidden to tray")

    def on_quit(self):
        self.engine.stop()
        if self.global_hotkey_listener:
            self.global_hotkey_listener.stop()
            self.global_hotkey_listener = None
        if self.tray_icon:
            self.tray_icon.stop()
            self.tray_icon = None
        self.overlay.hide()
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


def is_valid_discord_webhook(url):
    try:
        parsed = urlparse(url.strip())
        if parsed.scheme != "https":
            return False
        if parsed.hostname not in ALLOWED_WEBHOOK_HOSTS:
            return False
        return parsed.path.startswith("/api/webhooks/") and len(parsed.path.split("/")) >= 5
    except Exception:
        return False


if __name__ == "__main__":
    main()
