@echo off
REM Builds Airzone.exe — a Windows desktop app.
REM Run from the project root folder.
REM
REM Requirements:
REM   pip install pyinstaller pyqt5 matplotlib requests openpyxl keyring
REM
REM Usage:
REM   scripts\build_windows.bat

cd /d "%~dp0\.."

echo Building Airzone.exe ...

python -m PyInstaller ^
    --windowed ^
    --name "Airzone" ^
    --paths "src" ^
    --hidden-import "airzone_humidity_controller" ^
    --hidden-import "airzone_control_brain" ^
    --hidden-import "airzone_thermal_model" ^
    --hidden-import "airzone_baseline" ^
    --hidden-import "airzone_best_price" ^
    --hidden-import "airzone_supabase" ^
    --hidden-import "airzone_weather" ^
    --hidden-import "airzone_analytics" ^
    --hidden-import "airzone_linky" ^
    --hidden-import "airzone_netatmo" ^
    --hidden-import "airzone_secrets" ^
    --hidden-import "keyring" ^
    --hidden-import "keyring.backends" ^
    --hidden-import "keyring.backends.Windows" ^
    --hidden-import "openpyxl" ^
    --hidden-import "PyQt5" ^
    --hidden-import "matplotlib" ^
    --hidden-import "matplotlib.backends.backend_qt5agg" ^
    --noconfirm ^
    src\airzone_app.py

echo.
echo Done!  App is at:  dist\Airzone\Airzone.exe
echo.
echo To use:
echo   - Double-click dist\Airzone\Airzone.exe
echo   - Keep airzone_config.json in the same folder as the .exe
echo   - Or copy the entire dist\Airzone folder wherever you like
echo.
pause
