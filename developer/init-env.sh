#!/bin/bash

echo "Run to init the development environment required to run the dev tests."

python -m pip install --upgrade pip
pip install pylint
pip install pyright
pip install ruff
pip install octoprint
pip install -r ../requirements.txt
pip install "zstandard>=0.21.0,<0.23.0"