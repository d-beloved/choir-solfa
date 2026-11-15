@echo off
rem Installs Choir Solfa: https://d-beloved.github.io/choir-solfa/
rem It runs the same installer as the PowerShell line on that page.
echo Installing Choir Solfa. This takes a few minutes the first time...
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/d-beloved/choir-solfa/main/install.ps1 | iex"
echo.
pause
