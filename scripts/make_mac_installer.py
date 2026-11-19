"""Build dist/Install-Choir-Solfa-Mac.zip: a small app that installs Choir Solfa when double-clicked.

    python3 scripts/make_mac_installer.py

Needs a Mac (it uses osacompile, codesign and ditto). CI builds it and publishes it to the
"mac-installer" GitHub release, which the website links to (.github/workflows/mac-installer.yml).

The app is a compiled AppleScript applet, so Apple's notary service accepts it. When opened,
it runs launch.sh from its Resources folder, which writes a .command file and opens it with
Terminal: people can watch the install, and it always runs the latest install.sh from GitHub.
Opening a file needs no extra permission, unlike asking Terminal to run a command.

Set MACOS_SIGN_IDENTITY (a "Developer ID Application" identity in the keychain) to sign it.
Notarizing and stapling happen in the workflow, after this script.
"""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
ICON = ROOT / "src" / "choir_solfa" / "assets" / "icon.icns"
APP = "Install Choir Solfa.app"
ZIP = "Install-Choir-Solfa-Mac.zip"
INSTALL_URL = "https://raw.githubusercontent.com/d-beloved/choir-solfa/main/install.sh"

APPLESCRIPT = """\
-- Install Choir Solfa: runs launch.sh, which opens Terminal on the installer.
do shell script "/bin/sh " & quoted form of POSIX path of (path to resource "launch.sh")
"""

LAUNCHER = f"""#!/bin/sh
# Opens Terminal and runs the Choir Solfa installer there, so you can watch it.
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


def run(*cmd: str | Path) -> None:
    subprocess.run([str(c) for c in cmd], check=True)


def build_app(out: Path) -> Path:
    """Make the app in `out`, signed when MACOS_SIGN_IDENTITY is set. Returns its path."""
    app = out / APP
    shutil.rmtree(app, ignore_errors=True)
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "install.applescript"
        source.write_text(APPLESCRIPT)
        run("osacompile", "-o", app, source)

    resources = app / "Contents" / "Resources"
    launcher = resources / "launch.sh"
    launcher.write_text(LAUNCHER)
    launcher.chmod(0o755)
    shutil.copyfile(ICON, resources / "applet.icns")

    plist = app / "Contents" / "Info.plist"
    info = plistlib.loads(plist.read_bytes())
    info.update({
        "CFBundleName": "Install Choir Solfa",
        "CFBundleDisplayName": "Install Choir Solfa",
        "CFBundleIdentifier": "io.github.d-beloved.choir-solfa.installer",
        "CFBundleShortVersionString": "1.1",
        "CFBundleVersion": "2",
        "LSMinimumSystemVersion": "11.0",
        "LSUIElement": True,  # it only opens Terminal, so no Dock icon
    })
    plist.write_bytes(plistlib.dumps(info))

    identity = os.environ.get("MACOS_SIGN_IDENTITY")
    if identity:
        run("codesign", "--force", "--deep", "--options", "runtime", "--timestamp", "--sign", identity, app)
    else:
        run("codesign", "--force", "--deep", "--sign", "-", app)  # ad hoc: it runs, with a warning when downloaded
    run("codesign", "--verify", "--strict", "--verbose=2", app)
    return app


def zip_app(app: Path) -> Path:
    archive = app.parent / ZIP
    archive.unlink(missing_ok=True)
    run("ditto", "-c", "-k", "--keepParent", app, archive)  # ditto keeps the signature intact
    return archive


if __name__ == "__main__":
    if sys.platform != "darwin":
        sys.exit("This needs a Mac.")
    DIST.mkdir(exist_ok=True)
    archive = zip_app(build_app(DIST))
    print(f"wrote {archive.relative_to(ROOT)} ({archive.stat().st_size // 1024} KB)")
