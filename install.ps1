# Install Choir Solfa on Windows. Paste this into PowerShell (Start menu > type "PowerShell"):
#
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/d-beloved/choir-solfa/main/install.ps1 | iex"
#
# It installs uv (a Python installer) if needed, then Choir Solfa with its own Python,
# and adds "Choir Solfa" to the Start menu and the desktop. Run it again to update.
$ErrorActionPreference = "Stop"

$Source = if ($env:CHOIR_SOLFA_SOURCE) { $env:CHOIR_SOLFA_SOURCE } else { "https://github.com/d-beloved/choir-solfa/archive/refs/heads/main.zip" }
# Use a Python that uv downloads, never another one already on the computer.
$env:UV_PYTHON_PREFERENCE = "only-managed"

function Say($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Fail($msg) { Write-Host "`nSorry: $msg" -ForegroundColor Red; exit 1 }

if (-not [Environment]::Is64BitOperatingSystem) { Fail "Choir Solfa needs 64-bit Windows." }

$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { $uv = Join-Path $env:USERPROFILE ".local\bin\uv.exe" }
if (-not (Test-Path $uv)) {
    Say "Getting uv, which installs Python and Choir Solfa"
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    if (-not (Test-Path $uv)) { Fail "uv did not install. Check your internet connection and try again." }
}

Say "Installing Choir Solfa (the first time downloads about 1.5 GB, so it can take several minutes)"
& $uv tool install --force --python 3.11 --reinstall-package choir-solfa $Source
if ($LASTEXITCODE -ne 0) { Fail "Choir Solfa did not install. Check your internet connection and try again." }

$bin = Join-Path (& $uv tool dir --bin) "choir-solfa.exe"
if (-not (Test-Path $bin)) { Fail "the install finished, but $bin is missing." }

Say "Adding Choir Solfa to the Start menu and the desktop"
& $bin shortcut
if ($LASTEXITCODE -ne 0) { Fail "Choir Solfa installed, but the shortcut could not be made. Run 'choir-solfa' to start it." }

Say "Done"
Write-Host "Open Choir Solfa from the Start menu or the desktop. It opens in your web browser."
Write-Host "Your songs stay on this computer, in $env:USERPROFILE\.choir-solfa."
Write-Host "The first song takes longer, because it downloads the voice separation model (about 80 MB)."
Write-Host ""
Write-Host "To update later, run the same install line again."

if (-not $env:CHOIR_SOLFA_NO_OPEN) {
    $link = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Choir Solfa.lnk"
    Start-Process $link
}
