# Windows Tray Build

This branch packages Tessera Monitoring and Control as a Windows tray application.

The tray process starts three child service modes of the same executable:

- FastAPI web interface on `http://127.0.0.1:8080/`
- Tessera TCP API simulator on TCP port `23`
- UDP syslog collector on UDP port `514`

## Build

Run this from a Windows PowerShell prompt:

```powershell
.\scripts\build_windows.ps1
```

The script creates `.venv-windows`, installs the Windows packaging dependencies, converts the supplied S.L PNG icon into a multi-size `.ico`, and runs PyInstaller.

The output is:

```text
dist\TesseraMonitoringAndControl.exe
```

## Running

Double-click `dist\TesseraMonitoringAndControl.exe`.

The app will start in the system tray. The tray menu can:

- Open the web interface
- Open the local data folder
- Start or stop the server processes
- Quit the tray app

The local data folder defaults to:

```text
%ProgramData%\TesseraMonitoringAndControl
```

You can override it with `TESSERA_SIM_BASE`.

## Ports And Permissions

The app uses TCP `23` and UDP `514` to match Tessera processor behavior. On some Windows systems, binding to these ports or accepting firewall traffic may require running the executable as Administrator and allowing Windows Defender Firewall prompts.

The HTTP web interface port can be changed with `PORT`.
The TCP API port can be changed with `TESSERA_TCP_PORT`.
The UDP syslog port can be changed with `TESSERA_SYSLOG_PORT`.
