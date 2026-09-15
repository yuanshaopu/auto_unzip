@echo off
rem ============================================================
rem  auto_unzip launcher
rem  Double-click this file where your archives are located.
rem  It runs auto_unzip.exe located in the SAME folder as this
rem  launcher, and that folder is used as the target directory.
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0"

if not exist "%~dp0auto_unzip.exe" (
    echo auto_unzip.exe not found in this folder.
    echo Please keep auto_unzip.exe next to this launcher.
    echo.
    pause
    exit /b 1
)

rem Use "%~dp0." (trailing dot) to avoid the trailing backslash
rem escaping the closing quote in the argument.
"%~dp0auto_unzip.exe" "%~dp0."
echo.
echo --- Finished. Window will close in 8 seconds ---
timeout /t 8 >nul
