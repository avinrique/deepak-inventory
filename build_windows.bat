@echo off
REM Always operate on this script's own folder, whatever the caller's cwd is.
cd /d "%~dp0"
REM ==========================================================================
REM  Build a standalone Windows .exe for the Inventory Management app.
REM  Run this ON A WINDOWS MACHINE (double-click it, or run from cmd).
REM
REM  Requires Python 3.x installed (python.org). It installs the needed
REM  packages, then produces dist\InventoryManagement.exe
REM ==========================================================================

echo Installing dependencies...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo ERROR: pip install failed. Check that Python is installed and on
    echo        PATH, and that requirements.txt sits next to this script.
    pause
    exit /b 1
)

python -m pip install "pyinstaller>=6.0"
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo ERROR: could not install PyInstaller.
    pause
    exit /b 1
)

REM Remove the previous output so a failed build cannot leave a stale .exe
REM that the check below would then report as freshly built.
if exist dist rmdir /s /q dist

echo.
echo Building InventoryManagement.exe ...
python -m PyInstaller --onefile --windowed --name InventoryManagement inventory_app.py
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo ==========================================================================
    echo  BUILD FAILED. dist\InventoryManagement.exe was NOT updated.
    echo  Scroll up for the PyInstaller error. Do not ship the old .exe as new.
    echo ==========================================================================
    pause
    exit /b 1
)

if not exist "dist\InventoryManagement.exe" (
    echo.
    echo ERROR: PyInstaller reported success but dist\InventoryManagement.exe
    echo        is missing. Something went wrong -- do not ship this build.
    pause
    exit /b 1
)

echo.
echo ==========================================================================
echo  Done. Your app is here:  dist\InventoryManagement.exe
echo  Double-click it to run. Excel files are stored in
echo  %%APPDATA%%\InventoryManagement\inventory_data\ so rebuilding or
echo  moving the .exe never loses your data.
echo ==========================================================================
pause
