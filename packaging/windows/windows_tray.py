#!/usr/bin/env python3
"""Windows system tray launcher for Tessera Monitoring and Control."""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Iterable

from PIL import Image
import pystray
from pystray import MenuItem as Item


APP_NAME = "Tessera Monitoring and Control"
HTTP_PORT = int(os.environ.get("PORT", "8080"))
TCP_PORT = int(os.environ.get("TESSERA_TCP_PORT", "23"))
SYSLOG_PORT = int(os.environ.get("TESSERA_SYSLOG_PORT", "514"))


def frozen_base() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parents[2]


def app_dir() -> Path:
    return frozen_base() / "app"


def data_dir() -> Path:
    default_base = Path(os.environ.get("PROGRAMDATA", Path.home())) / "TesseraMonitoringAndControl"
    return Path(os.environ.get("TESSERA_SIM_BASE", default_base))


def configure_environment() -> None:
    os.environ.setdefault("TESSERA_SIM_BASE", str(data_dir()))
    os.environ.setdefault("PORT", str(HTTP_PORT))
    os.environ.setdefault("TESSERA_TCP_PORT", str(TCP_PORT))
    os.environ.setdefault("TESSERA_SYSLOG_PORT", str(SYSLOG_PORT))
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
    uvicorn.run(app, host="0.0.0.0", port=HTTP_PORT, log_level="info")


def run_syslog_service() -> None:
    configure_environment()
    import syslog_server

    asyncio.run(syslog_server.main())


class TrayController:
    def __init__(self) -> None:
        self.processes: dict[str, subprocess.Popen] = {}
        self.icon: pystray.Icon | None = None

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
        webbrowser.open(f"http://127.0.0.1:{HTTP_PORT}/")

    def open_data_folder(self) -> None:
        folder = data_dir()
        folder.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(folder)  # type: ignore[attr-defined]
        else:
            webbrowser.open(folder.as_uri())

    def quit(self) -> None:
        self.stop_all()
        if self.icon:
            self.icon.stop()

    def menu(self) -> pystray.Menu:
        running = self.is_running()
        return pystray.Menu(
            Item(self.status_text(), None, enabled=False),
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
        self.start_all()
        self.icon.run()


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
