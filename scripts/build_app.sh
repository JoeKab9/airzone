#!/bin/bash
# Builds Airzone.app — a proper macOS double-click app.
# Run from anywhere; the .app appears in dist/.
#
# Requirements:  python3 -m pip install pyinstaller
#
# Usage:
#   bash scripts/build_app.sh

set -e
cd "$(dirname "$0")/.."

echo "Building Airzone.app ..."

python3 -m PyInstaller \
    --windowed \
    --name "Airzone" \
    --icon "icons/Airzone.icns" \
    --paths "src" \
    --hidden-import "airzone_humidity_controller" \
    --hidden-import "airzone_control_brain" \
    --hidden-import "airzone_thermal_model" \
    --hidden-import "airzone_baseline" \
    --hidden-import "airzone_best_price" \
    --hidden-import "airzone_weather" \
    --hidden-import "airzone_analytics" \
    --hidden-import "airzone_linky" \
    --hidden-import "airzone_netatmo" \
    --hidden-import "airzone_secrets" \
    --hidden-import "keyring" \
    --hidden-import "keyring.backends" \
    --hidden-import "keyring.backends.macOS" \
    --hidden-import "openpyxl" \
    --hidden-import "PyQt5" \
    --hidden-import "matplotlib" \
    --hidden-import "matplotlib.backends.backend_qt5agg" \
    --noconfirm \
    src/airzone_app.py

echo ""
echo "Done!  App is at:  dist/Airzone.app"
echo ""
echo "To use:"
echo "  • Double-click dist/Airzone.app  (keep airzone_config.json in the same folder)"
echo "  • Or drag Airzone.app to your Applications folder"
echo "    (airzone_config.json will be created next to the .app on first run)"
