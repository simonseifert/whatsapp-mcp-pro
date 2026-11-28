@echo off
rem wactl launcher: runs scripts\wactl.py with uv.
uv run --quiet --no-project "%~dp0scripts\wactl.py" %*
exit /b %ERRORLEVEL%
