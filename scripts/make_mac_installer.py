"""Build docs/Install-Choir-Solfa-Mac.zip: a small app that installs Choir Solfa when double-clicked.

    .venv/bin/python scripts/make_mac_installer.py

The app opens a Terminal window that runs install.sh from GitHub, so people can watch it
work and it always installs the latest version. It holds no Python and never goes stale.

The zip is built the same way every time (fixed dates and order), so a test can check that
the copy in docs/ matches this script. It is not signed by Apple, so the first open shows
a warning; see the website for the steps.
"""
from __future__ import annotations

import io
import plistlib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "Install-Choir-Solfa-Mac.zip"
ICON = ROOT / "src" / "choir_solfa" / "assets" / "icon.icns"
APP = "Install Choir Solfa.app"
INSTALL_URL = "https://raw.githubusercontent.com/d-beloved/choir-solfa/main/install.sh"

# The app's program. It writes a .command file and opens it with Terminal: opening a file
# needs no extra permission, unlike asking Terminal to run a command.
LAUNCHER = f"""#!/bin/sh
# Install Choir Solfa: opens Terminal and runs the installer there, so you can watch it.
URL="${{CHOIR_SOLFA_INSTALL_URL:-{INSTALL_URL}}}"
DIR="$(mktemp -d)"
SCRIPT="$DIR/Installing Choir Solfa.command"
cat > "$SCRIPT" <<EOF
#!/bin/sh
clear
printf '\\n  Installing Choir Solfa\\n\\n'
echo "  This window shows how it is going. Please leave it open."
echo "  The first time downloads about 1 GB, so it can take several minutes."
curl -LsSf "$URL" | sh
if [ \\$? -eq 0 ]; then
  printf '\\n  All done. Choir Solfa is in Launchpad, and it should be opening in your browser now.\\n'
  echo "  You can close this window and move the installer to the Bin."
else
  printf '\\n  Choir Solfa did not install. Check your internet connection, then open the installer again.\\n'
fi
EOF
chmod +x "$SCRIPT"
open -a Terminal "$SCRIPT"
"""

INFO = {
    "CFBundleName": "Install Choir Solfa",
    "CFBundleDisplayName": "Install Choir Solfa",
    "CFBundleIdentifier": "io.github.d-beloved.choir-solfa.installer",
    "CFBundleExecutable": "install",
    "CFBundleIconFile": "icon",
    "CFBundlePackageType": "APPL",
    "CFBundleShortVersionString": "1.0",
    "CFBundleVersion": "1",
    "LSMinimumSystemVersion": "11.0",
    "LSUIElement": True,  # it only opens Terminal, so no Dock icon
}

DATE = (2026, 1, 1, 0, 0, 0)


def _add(z: zipfile.ZipFile, name: str, data: bytes | None, mode: int) -> None:
    info = zipfile.ZipInfo(name, DATE)
    info.create_system = 3  # Unix, so the permission bits below are kept when unzipped
    if data is None:
        info.external_attr = (0o40755 << 16) | 0x10
        z.writestr(info, b"")
    else:
        info.external_attr = mode << 16
        info.compress_type = zipfile.ZIP_DEFLATED
        z.writestr(info, data)


def build() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for d in ("", "Contents/", "Contents/MacOS/", "Contents/Resources/"):
            _add(z, f"{APP}/{d}", None, 0)
        _add(z, f"{APP}/Contents/Info.plist", plistlib.dumps(INFO, sort_keys=True), 0o100644)
        _add(z, f"{APP}/Contents/MacOS/install", LAUNCHER.encode(), 0o100755)
        _add(z, f"{APP}/Contents/Resources/icon.icns", ICON.read_bytes(), 0o100644)
    return buf.getvalue()


if __name__ == "__main__":
    OUT.write_bytes(build())
    print(f"wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size // 1024} KB)")
