cd ..
echo "Testing OctoEverywhere Module..."
pylint ./octoeverywhere/
echo "Testing OctoPrint Module..."
pylint ./octoprint_octoeverywhere/
echo "Testing Moonraker Module..."
pylint ./moonraker_octoeverywhere/
echo "Testing Linux Host Module..."
pylint ./linux_host/
echo "Testing Bambu Module..."
pylint ./bambu_octoeverywhere/
echo "Testing Moonraker Installer Module..."
pylint ./py_installer/