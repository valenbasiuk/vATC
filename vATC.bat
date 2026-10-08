@echo off
rem vATC launcher: the setup window (python -m atc.gui), without a console window.
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" -m atc.gui
) else (
    start "" pythonw -m atc.gui
)
