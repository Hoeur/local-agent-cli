@echo off
title Local Agent
cd /d "%~dp0"
echo Using:
python --version
python -c "import textual, psutil" 2>nul
if errorlevel 1 (
    echo Installing the terminal UI dependencies...
    python -m pip install -e ".[tui]"
    if errorlevel 1 (
        pause
        exit /b 1
    )
)
python -m local_agent chat --session demo --workspace . %*
if errorlevel 1 pause
