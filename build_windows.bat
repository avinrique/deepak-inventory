@echo off
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
if errorlevel 1 (
    echo.
    echo ERROR: pip install failed. Make sure Python is installed and on PATH.
    pause
    exit /b 1
)

echo.
echo Building InventoryManagement.exe ...
python -m PyInstaller --onefile --windowed --name InventoryManagement inventory_app.py

echo.
echo ==========================================================================
echo  Done. Your app is here:  dist\InventoryManagement.exe
echo  Double-click it to run. Excel files are stored in
echo  %%APPDATA%%\InventoryManagement\inventory_data\ so rebuilding or
echo  moving the .exe never loses your data.
echo ==========================================================================
pause
