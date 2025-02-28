cd ..

echo "Ensuring required PY packages are installed..."
pip install pylint octoprint==1.9.0
pip install -r requirements.txt

echo ""
echo ""
echo ""
echo "Testing OctoEverywhere Module..."
pylint ./octoeverywhere/

echo "Testing OctoPrint Module..."
pylint ./octoprint_octoeverywhere/
echo "Testing Moonraker Module..."
pylint ./moonraker_octoeverywhere/
echo "Testing Bambu Module..."
pylint ./bambu_octoeverywhere/
echo "Testing Elegoo Module..."
pylint ./elegoo_octoeverywhere/

echo "Testing Linux Host Module..."
pylint ./linux_host/
echo "Testing Moonraker Installer Module..."
pylint ./py_installer/

cd developer
