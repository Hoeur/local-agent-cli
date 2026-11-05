@echo off
title Local Agent
cd /d "%~dp0"
echo Using:
python --version
python -c "import textual" 2>nul || (echo Installing the terminal UI dependency... & python -m pip install "textual>=8.0,<9")
python -m local_agent chat --session demo --workspace . %*
if errorlevel 1 pause
