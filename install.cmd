@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "%~dp0scripts\install.py" (
  echo This launcher needs the complete uibe-lecture-mcp folder.
  echo Use UIBE-Lecture-Setup.cmd for the standalone installer.
  pause
  exit /b 1
)
python "%~dp0scripts\install.py" %*
set "RC=%ERRORLEVEL%"
echo.
pause

exit /b %RC%
