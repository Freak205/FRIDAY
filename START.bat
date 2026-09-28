@echo off
REM Start FRIDAY in the background: tray icon, Ctrl+Alt+Space command bar,
REM Ctrl+Alt+V push-to-talk, and (if voice.wakeword.enabled in config.yaml,
REM the default) wake-word listening — say "hey jarvis" and just start
REM talking, no hotkey needed. See PLAN.md Phase 8 for what that placeholder
REM phrase means and MANUAL_VALIDATION.md for how to test it. If wake-word
REM listening can't start for any reason, FRIDAY still starts normally and
REM Ctrl+Alt+V still works — check data/logs/friday.log for why.
REM Safe to double-click, or to launch from a shortcut anywhere (Desktop,
REM Start Menu, startup folder) ? every path below is absolute, derived from
REM this file's own location. `start` looks up bare program names on PATH, not
REM in the current directory, so a relative path here would fail.

setlocal
set "ROOT=%~dp0"
set "PYW=%ROOT%.venv\Scripts\pythonw.exe"

if not exist "%PYW%" (
    echo.
    echo   FRIDAY's virtual environment is missing:
    echo     %PYW%
    echo.
    echo   Create it once with:
    echo     py -3.12 -m venv "%ROOT%.venv"
    echo     "%ROOT%.venv\Scripts\python.exe" -m pip install -r "%ROOT%requirements.txt"
    echo.
    pause
    exit /b 1
)

start "FRIDAY" /D "%ROOT%" "%PYW%" "%ROOT%run.py" desktop
exit /b 0
