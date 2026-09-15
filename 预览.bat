@echo off
rem ============================================================
rem  auto_unzip PREVIEW mode (dry-run)
rem  Double-click to only SCAN and list what would be processed.
rem  It does NOT extract, move, or create anything.
rem  After confirming, use the other launcher to actually run.
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
"%~dp0auto_unzip.exe" --dry-run "%~dp0."
echo.
echo --- Preview finished. Window will close in 10 seconds ---
timeout /t 10 >nul
