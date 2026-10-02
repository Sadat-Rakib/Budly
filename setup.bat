@echo off
REM Budly setup helper (Windows). Checks the tools, installs Budly, prepares .env.
setlocal

echo === Budly setup ===

python --version >nul 2>&1
if errorlevel 1 (
    echo [MISSING] Python 3.12+ is required. Install from https://www.python.org/downloads/
    exit /b 1
) else (
    for /f "tokens=2" %%v in ('python --version 2^>^&1') do echo [ok] Python %%v
)

uv --version >nul 2>&1
if errorlevel 1 (
    echo [MISSING] uv is required. Install with:
    echo     powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    exit /b 1
) else (
    for /f "tokens=1-2" %%a in ('uv --version') do echo [ok] %%a %%b
)

echo [..] Installing dependencies...
uv sync
if errorlevel 1 (
    echo [FAIL] uv sync failed. See the output above.
    exit /b 1
)
echo [ok] Dependencies installed.

if exist .env (
    echo [ok] .env already exists - keeping it.
) else (
    copy .env.example .env >nul
    echo [ok] Created .env from .env.example.
    echo      Next: open .env and add your Canvas URL and access token.
)

echo.
echo === Done ===
echo Start Budly with:   uv run budly start
echo Then open:          http://127.0.0.1:8000
echo Check Canvas with:  uv run budly test-canvas
