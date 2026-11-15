"""Make (or remove) the icon people double-click to open Choir Solfa.

- macOS: "Choir Solfa.app" in ~/Applications, so it shows in Launchpad and Spotlight.
- Windows: "Choir Solfa" in the Start menu and on the desktop.

Clicking it starts the page server in the background with no terminal window and
opens the page. The server quits on its own 15 minutes after the last page closes,
and a second click while it is running just opens the page again.
"""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

NAME = "Choir Solfa"
ASSETS = Path(__file__).parent / "assets"


def _home() -> Path:
    return Path(os.environ.get("CHOIR_SOLFA_HOME", Path.home() / ".choir-solfa"))


# ---------- macOS ----------

def mac_app_path() -> Path:
    return Path.home() / "Applications" / f"{NAME}.app"


def mac_launcher(python: str, log: Path) -> str:
    q = lambda s: "'" + str(s).replace("'", "'\\''") + "'"  # noqa: E731 - shell single-quote
    return f"""#!/bin/sh
# Starts Choir Solfa in the background and opens the page. Made by `choir-solfa shortcut`.
mkdir -p {q(log.parent)}
nohup {q(python)} -m choir_solfa serve --quit-when-closed >> {q(log)} 2>&1 &
"""


def make_mac(python: str) -> Path:
    app = mac_app_path()
    if app.exists():
        shutil.rmtree(app)
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Resources").mkdir()
    exe = app / "Contents" / "MacOS" / "choir-solfa"
    exe.write_text(mac_launcher(python, _home() / "app.log"), encoding="utf-8")
    exe.chmod(0o755)
    shutil.copyfile(ASSETS / "icon.icns", app / "Contents" / "Resources" / "icon.icns")
    info = {
        "CFBundleName": NAME,
        "CFBundleDisplayName": NAME,
        "CFBundleIdentifier": "io.github.d-beloved.choir-solfa",
        "CFBundleExecutable": "choir-solfa",
        "CFBundleIconFile": "icon.icns",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": _version(),
        "LSMinimumSystemVersion": "11.0",
        "LSUIElement": True,  # the launcher exits at once; don't flash a Dock icon
    }
    with open(app / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump(info, f)
    # Ask Finder to notice the new icon straight away.
    subprocess.run(["touch", str(app)], check=False)
    return app


# ---------- Windows ----------

def windows_places() -> list[Path]:
    appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    places = [appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / f"{NAME}.lnk"]
    desktop = Path.home() / "Desktop"
    if desktop.is_dir():
        places.append(desktop / f"{NAME}.lnk")
    return places


def make_windows(python: str) -> list[Path]:
    pythonw = Path(python).with_name("pythonw.exe")
    target = pythonw if pythonw.exists() else Path(python)  # pythonw: no console window
    icon = ASSETS / "icon.ico"
    made = []
    for link in windows_places():
        link.parent.mkdir(parents=True, exist_ok=True)
        ps = (
            "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:CS_LINK);"
            "$s.TargetPath = $env:CS_TARGET;"
            "$s.Arguments = '-m choir_solfa serve --quit-when-closed';"
            "$s.WorkingDirectory = $env:USERPROFILE;"
            "$s.IconLocation = $env:CS_ICON;"
            "$s.Description = 'Turn a song into tonic solfa for SATB';"
            "$s.Save()"
        )
        env = {**os.environ, "CS_LINK": str(link), "CS_TARGET": str(target), "CS_ICON": str(icon)}
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], env=env, check=True)
        made.append(link)
    return made


# ---------- both ----------

def _version() -> str:
    try:
        from importlib.metadata import version
        return version("choir-solfa")
    except Exception:
        return "0"


def make(python: str | None = None) -> list[Path]:
    python = python or sys.executable
    if sys.platform == "darwin":
        return [make_mac(python)]
    if sys.platform == "win32":
        return make_windows(python)
    raise SystemExit("Shortcuts are made on macOS and Windows only. Run `choir-solfa` to start the page.")


def remove() -> list[Path]:
    places = [mac_app_path()] if sys.platform == "darwin" else windows_places() if sys.platform == "win32" else []
    gone = []
    for p in places:
        if p.is_dir():
            shutil.rmtree(p)
            gone.append(p)
        elif p.exists():
            p.unlink()
            gone.append(p)
    return gone
