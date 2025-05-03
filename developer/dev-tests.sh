#!/bin/bash

cd ..

echo ""
echo "Running pylint on the OctoEverywhere Module..."
pylint ./octoeverywhere/
echo "Running pylint on the OctoPrint Module..."
pylint ./octoprint_octoeverywhere/
echo "Running pylint on the Moonraker Module..."
pylint ./moonraker_octoeverywhere/
echo "Running pylint on the Elegoo Module..."
pylint ./elegoo_octoeverywhere/
echo "Running pylint on the Bambu Module..."
pylint ./bambu_octoeverywhere/
echo "Running pylint on the Linux Host Module..."
pylint ./linux_host/
echo "Running pylint on the Installer Module..."
pylint ./py_installer/
echo "Running pylint on the Docker Module..."
pylint ./docker_octoeverywhere/

echo "Running pyright..."
pyright

echo "Running ruff..."
ruff check

cd developer