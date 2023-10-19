cd ..
echo "Testing OctoEverywhere Module..."
pylint ./octoeverywhere/
echo "Testing OctoPrint Module..."
pylint ./octoprint_octoeverywhere/
echo "Testing Moonraker Module..."
pylint ./moonraker_octoeverywhere/
echo "Testing Moonraker Installer Module..."
pylint ./moonraker_installer/