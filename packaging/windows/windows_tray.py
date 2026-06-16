#!/usr/bin/env python3
"""Windows system tray launcher for Tessera Monitoring and Control."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Iterable

from PIL import Image
import pystray
from pystray import MenuItem as Item
import tkinter as tk
from tkinter import messagebox, ttk


APP_NAME = "Tessera Monitoring and Control"
TCP_PORT = int(os.environ.get("TESSERA_TCP_PORT", "23"))
SYSLOG_PORT = int(os.environ.get("TESSERA_SYSLOG_PORT", "514"))
DEFAULT_HTTP_HOST = "0.0.0.0"
DEFAULT_HTTP_PORT = 8080


def frozen_base() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parents[2]


def app_dir() -> Path:
    return frozen_base() / "app"


def data_dir() -> Path:
    default_base = Path(os.environ.get("PROGRAMDATA", Path.home())) / "TesseraMonitoringAndControl"
    return Path(os.environ.get("TESSERA_SIM_BASE", default_base))


def settings_path() -> Path:
    return data_dir() / "windows_settings.json"


def load_settings() -> dict:
    defaults = {
        "http_host": os.environ.get("TESSERA_HTTP_HOST", DEFAULT_HTTP_HOST),
        "http_port": int(os.environ.get("PORT", str(DEFAULT_HTTP_PORT))),
        "hide_on_launch": False,
    }
    try:
        with settings_path().open("r", encoding="utf-8") as handle:
            saved = json.load(handle)
        if isinstance(saved, dict):
            defaults.update(saved)
    except Exception:
        pass
    try:
        defaults["http_port"] = max(1, min(int(defaults.get("http_port") or DEFAULT_HTTP_PORT), 65535))
    except Exception:
        defaults["http_port"] = DEFAULT_HTTP_PORT
    defaults["http_host"] = str(defaults.get("http_host") or DEFAULT_HTTP_HOST)
    defaults["hide_on_launch"] = bool(defaults.get("hide_on_launch"))
    return defaults


def save_settings(settings: dict) -> None:
    data_dir().mkdir(parents=True, exist_ok=True)
    with settings_path().open("w", encoding="utf-8") as handle:
        json.dump(settings, handle, indent=2)


def http_url(settings: dict | None = None) -> str:
    settings = settings or load_settings()
    host = settings.get("http_host") or DEFAULT_HTTP_HOST
    display_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    return f"http://{display_host}:{int(settings.get('http_port') or DEFAULT_HTTP_PORT)}/"


def local_interfaces() -> list[tuple[str, str]]:
    choices = [("All Interfaces", "0.0.0.0"), ("Localhost", "127.0.0.1")]
    seen = {value for _, value in choices}
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_DGRAM):
            ip = info[4][0]
            if ip not in seen and not ip.startswith("127."):
                choices.append((ip, ip))
                seen.add(ip)
    except Exception:
        pass
    return choices


def configure_stdio() -> None:
    log_dir = data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    if sys.stdout is None:
        sys.stdout = open(log_dir / "stdout.log", "a", encoding="utf-8", buffering=1)
    if sys.stderr is None:
        sys.stderr = open(log_dir / "stderr.log", "a", encoding="utf-8", buffering=1)


def configure_environment() -> None:
    settings = load_settings()
    os.environ.setdefault("TESSERA_SIM_BASE", str(data_dir()))
    os.environ.setdefault("TESSERA_APP_DIR", str(app_dir()))
    os.environ["TESSERA_HTTP_HOST"] = str(settings["http_host"])
    os.environ["PORT"] = str(settings["http_port"])
    os.environ.setdefault("TESSERA_TCP_PORT", str(TCP_PORT))
    os.environ.setdefault("TESSERA_SYSLOG_PORT", str(SYSLOG_PORT))
    configure_stdio()
    sys.path.insert(0, str(app_dir()))


def icon_path() -> Path:
    candidates = [
        frozen_base() / "packaging" / "windows" / "assets" / "sl-icon.png",
        Path(__file__).resolve().parent / "assets" / "sl-icon.png",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError("Could not find tray icon asset.")


async def run_tcp_service() -> None:
    configure_environment()
    from tessera_sim import handle_tcp, init_state

    init_state()
    server = await asyncio.start_server(handle_tcp, "0.0.0.0", TCP_PORT)
    async with server:
        await server.serve_forever()


def run_http_service() -> None:
    configure_environment()
    import uvicorn
    from tessera_sim import app, init_state

    init_state()
    settings = load_settings()
    uvicorn.run(app, host=str(settings["http_host"]), port=int(settings["http_port"]), log_config=None, access_log=False)


def run_syslog_service() -> None:
    configure_environment()
    import syslog_server

    asyncio.run(syslog_server.main())


class TrayController:
    def __init__(self) -> None:
        self.processes: dict[str, subprocess.Popen] = {}
        self.icon: pystray.Icon | None = None
        self.window: ControlWindow | None = None

    def child_command(self, service: str) -> list[str]:
        if getattr(sys, "frozen", False):
            return [sys.executable, f"--service={service}"]
        return [sys.executable, str(Path(__file__).resolve()), f"--service={service}"]

    def start_service(self, service: str) -> None:
        proc = self.processes.get(service)
        if proc and proc.poll() is None:
            return

        startupinfo = None
        creationflags = 0
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            creationflags = subprocess.CREATE_NO_WINDOW

        self.processes[service] = subprocess.Popen(
            self.child_command(service),
            cwd=str(frozen_base()),
            env=os.environ.copy(),
            startupinfo=startupinfo,
            creationflags=creationflags,
        )

    def start_all(self) -> None:
        configure_environment()
        data_dir().mkdir(parents=True, exist_ok=True)
        for service in ("http", "tcp", "syslog"):
            self.start_service(service)
        self.refresh_menu()

    def stop_all(self) -> None:
        for proc in self.processes.values():
            if proc.poll() is None:
                proc.terminate()
        deadline = time.time() + 5
        for proc in self.processes.values():
            while proc.poll() is None and time.time() < deadline:
                time.sleep(0.1)
            if proc.poll() is None:
                proc.kill()
        self.processes.clear()
        self.refresh_menu()

    def is_running(self) -> bool:
        return any(proc.poll() is None for proc in self.processes.values())

    def status_text(self) -> str:
        running = [name for name, proc in self.processes.items() if proc.poll() is None]
        if not running:
            return "Status: stopped"
        return "Status: " + ", ".join(sorted(running)) + " running"

    def open_web_ui(self) -> None:
        webbrowser.open(http_url())

    def toggle_window(self) -> None:
        if self.window:
            self.window.toggle()

    def hide_window(self) -> None:
        if self.window:
            self.window.hide()

    def restart_all(self) -> None:
        self.stop_all()
        self.start_all()

    def open_data_folder(self) -> None:
        folder = data_dir()
        folder.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(folder)  # type: ignore[attr-defined]
        else:
            webbrowser.open(folder.as_uri())

    def quit(self) -> None:
        if self.window and not self.window.confirm_quit():
            return
        self.stop_all()
        if self.icon:
            self.icon.stop()
        if self.window:
            self.window.destroy()

    def menu(self) -> pystray.Menu:
        running = self.is_running()
        return pystray.Menu(
            Item(self.status_text(), None, enabled=False),
            Item("Show/Hide Window", lambda *_: self.toggle_window()),
            Item("Open Web UI", lambda *_: self.open_web_ui(), enabled=running),
            Item("Open Data Folder", lambda *_: self.open_data_folder()),
            pystray.Menu.SEPARATOR,
            Item("Start Server", lambda *_: self.start_all(), enabled=not running),
            Item("Stop Server", lambda *_: self.stop_all(), enabled=running),
            pystray.Menu.SEPARATOR,
            Item("Quit", lambda *_: self.quit()),
        )

    def refresh_menu(self) -> None:
        if self.icon:
            self.icon.menu = self.menu()
            self.icon.update_menu()

    def run(self) -> None:
        configure_environment()
        image = Image.open(icon_path())
        self.icon = pystray.Icon(APP_NAME, image, APP_NAME, self.menu())
        self.window = ControlWindow(self)
        self.start_all()
        threading.Thread(target=self.icon.run, name="tray-icon", daemon=True).start()
        self.window.run()


class ControlWindow:
    def __init__(self, controller: TrayController) -> None:
        self.controller = controller
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("620x680")
        self.root.minsize(560, 620)
        self.root.configure(bg="#202020")
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self.settings = load_settings()
        self.interface_var = tk.StringVar(value=str(self.settings["http_host"]))
        self.port_var = tk.StringVar(value=str(self.settings["http_port"]))
        self.hide_on_launch_var = tk.BooleanVar(value=bool(self.settings.get("hide_on_launch")))
        self.status_var = tk.StringVar(value="Running")
        self.url_var = tk.StringVar(value=http_url(self.settings))
        self.build()
        if self.hide_on_launch_var.get():
            self.root.withdraw()

    def build(self) -> None:
        header = tk.Frame(self.root, bg="#202020")
        header.pack(fill="x", pady=(22, 18))
        logo = tk.Label(header, text="S.L", font=("Segoe UI", 48, "bold"), fg="#ffffff", bg="#202020")
        logo.pack()
        title = tk.Label(header, text=APP_NAME, font=("Segoe UI", 17, "bold"), fg="#ffffff", bg="#202020")
        title.pack(pady=(8, 0))

        body = tk.Frame(self.root, bg="#c90018")
        body.pack(fill="both", expand=True)
        tk.Label(body, textvariable=self.status_var, font=("Segoe UI", 38), fg="#f4a0a8", bg="#c90018").pack(pady=(24, 4))
        tk.Label(body, textvariable=self.url_var, font=("Segoe UI", 17, "bold"), fg="#ffffff", bg="#c90018").pack(pady=(0, 30))

        controls = tk.Frame(body, bg="#c90018")
        controls.pack(pady=8)
        tk.Label(controls, text="GUI Interface", font=("Segoe UI", 12, "bold"), fg="#ffffff", bg="#c90018").grid(row=0, column=0, sticky="w", padx=12)
        tk.Label(controls, text="Port", font=("Segoe UI", 12, "bold"), fg="#ffffff", bg="#c90018").grid(row=0, column=1, sticky="w", padx=12)

        self.interface_values = local_interfaces()
        self.interface_combo = ttk.Combobox(controls, values=[label for label, _ in self.interface_values], state="readonly", width=28)
        selected_label = next((label for label, value in self.interface_values if value == self.interface_var.get()), self.interface_values[0][0])
        self.interface_combo.set(selected_label)
        self.interface_combo.grid(row=1, column=0, padx=12, pady=4)

        port_frame = tk.Frame(controls, bg="#c90018")
        port_frame.grid(row=1, column=1, padx=12, pady=4)
        tk.Entry(port_frame, textvariable=self.port_var, width=8, font=("Segoe UI", 12)).pack(side="left")
        tk.Button(port_frame, text="Change", command=self.apply_settings, font=("Segoe UI", 11, "bold")).pack(side="left", padx=(6, 0))

        tk.Checkbutton(
            body,
            text="Hide this window on next launch",
            variable=self.hide_on_launch_var,
            command=self.save_window_preferences,
            font=("Segoe UI", 12, "bold"),
            fg="#ffffff",
            bg="#c90018",
            activebackground="#c90018",
            activeforeground="#ffffff",
            selectcolor="#202020",
        ).pack(pady=(28, 0))

        buttons = tk.Frame(body, bg="#c90018")
        buttons.pack(pady=(32, 0))
        tk.Button(buttons, text="Launch GUI", command=self.controller.open_web_ui, width=14, font=("Segoe UI", 15)).grid(row=0, column=0, padx=12)
        tk.Button(buttons, text="Hide", command=self.hide, width=10, font=("Segoe UI", 15, "bold")).grid(row=0, column=1, padx=12)
        tk.Button(buttons, text="Quit", command=self.controller.quit, width=10, font=("Segoe UI", 15, "bold")).grid(row=0, column=2, padx=12)

    def current_settings(self) -> dict:
        return {
            "http_host": str(self.settings.get("http_host") or DEFAULT_HTTP_HOST),
            "http_port": int(self.settings.get("http_port") or DEFAULT_HTTP_PORT),
            "hide_on_launch": bool(self.hide_on_launch_var.get()),
        }

    def save_window_preferences(self) -> None:
        settings = self.current_settings()
        save_settings(settings)
        self.settings = settings

    def apply_settings(self) -> None:
        label = self.interface_combo.get()
        host = next((value for item_label, value in self.interface_values if item_label == label), DEFAULT_HTTP_HOST)
        try:
            port = max(1, min(int(self.port_var.get()), 65535))
        except Exception:
            messagebox.showerror(APP_NAME, "Port must be a number from 1 to 65535.")
            return
        save_settings({"http_host": host, "http_port": port, "hide_on_launch": bool(self.hide_on_launch_var.get())})
        self.settings = load_settings()
        self.url_var.set(http_url(self.settings))
        self.controller.restart_all()

    def show(self) -> None:
        self.root.deiconify()
        self.root.lift()

    def hide(self) -> None:
        self.root.withdraw()

    def toggle(self) -> None:
        self.root.after(0, self._toggle)

    def _toggle(self) -> None:
        if self.root.state() == "withdrawn":
            self.show()
        else:
            self.hide()

    def confirm_quit(self) -> bool:
        return bool(messagebox.askyesno(APP_NAME, "Quit Tessera Monitoring and Control completely?"))

    def destroy(self) -> None:
        self.root.after(0, self.root.destroy)

    def run(self) -> None:
        self.root.mainloop()


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--service", choices=("http", "tcp", "syslog"))
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.service == "http":
        run_http_service()
        return 0
    if args.service == "tcp":
        asyncio.run(run_tcp_service())
        return 0
    if args.service == "syslog":
        run_syslog_service()
        return 0

    TrayController().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
