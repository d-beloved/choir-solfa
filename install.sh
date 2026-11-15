#!/bin/sh
# Install Choir Solfa on a Mac. Paste this into Terminal:
#
#   curl -LsSf https://raw.githubusercontent.com/d-beloved/choir-solfa/main/install.sh | sh
#
# It installs uv (a Python installer) if needed, then Choir Solfa with its own Python,
# and adds "Choir Solfa" to your Applications. Run it again to update.
set -eu

SOURCE="${CHOIR_SOLFA_SOURCE:-https://github.com/d-beloved/choir-solfa/archive/refs/heads/main.zip}"
# Use a Python that uv downloads, never an older or Intel-only one already on the Mac.
export UV_PYTHON_PREFERENCE=only-managed

say() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
fail() { printf '\n\033[1;31mSorry: %s\033[0m\n' "$1" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || fail "this installer is for Macs. On Windows, use install.ps1 (see the README)."
if [ "$(uname -m)" != "arm64" ]; then
  echo "Note: this is an Intel Mac. Choir Solfa runs best on Apple Silicon (M1 or newer); on Intel it is slower."
fi

if command -v uv >/dev/null 2>&1; then
  UV="$(command -v uv)"
elif [ -x "$HOME/.local/bin/uv" ]; then
  UV="$HOME/.local/bin/uv"
else
  say "Getting uv, which installs Python and Choir Solfa"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  UV="$HOME/.local/bin/uv"
  [ -x "$UV" ] || fail "uv did not install. Check your internet connection and try again."
fi

say "Installing Choir Solfa (the first time downloads about 1 GB, so it can take several minutes)"
"$UV" tool install --force --python 3.11 --reinstall-package choir-solfa "$SOURCE" \
  || fail "Choir Solfa did not install. Check your internet connection and try again."

BIN="$("$UV" tool dir --bin)/choir-solfa"
[ -x "$BIN" ] || fail "the install finished, but $BIN is missing."

say "Adding Choir Solfa to your Applications"
"$BIN" shortcut

say "Done"
echo "Open Choir Solfa from Launchpad or Spotlight, or from the Applications folder in your home folder."
echo "It opens in your web browser. Your songs stay on this Mac, in ~/.choir-solfa."
echo "The first song takes longer, because it downloads the voice separation model (about 80 MB)."
echo
echo "To update later, run the same install line again."

if [ -z "${CHOIR_SOLFA_NO_OPEN:-}" ]; then
  open "$HOME/Applications/Choir Solfa.app"
fi
