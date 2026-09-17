@echo off
setlocal EnableExtensions DisableDelayedExpansion
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 2)" >nul 2>nul
if not errorlevel 1 goto :Py
python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 2)" >nul 2>nul
if not errorlevel 1 goto :Python
echo Python 3.10+ is required. Install Python and enable its launcher or PATH entry.
exit /b 2
:Py
py -3 "%~dp0portable_server.py" %*
exit /b %ERRORLEVEL%
:Python
python "%~dp0portable_server.py" %*
exit /b %ERRORLEVEL%
