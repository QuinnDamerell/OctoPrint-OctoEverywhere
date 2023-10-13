#!/bin/bash

#
# OctoEverywhere for Klipper!
# This script is used to UPDATE OctoEverywhere for Klipper.
#
# If you're trying to do a clean install, please run ./install.sh
#
# If you need help, feel free to contact us at support@octoeverywhere.com
#

c_default=$(echo -en "\e[39m")
c_green=$(echo -en "\e[92m")
c_yellow=$(echo -en "\e[93m")
c_magenta=$(echo -en "\e[35m")
c_red=$(echo -en "\e[91m")
c_cyan=$(echo -en "\e[96m")

echo ""
echo ""
echo -e "${c_yellow}Starting an OctoEverywhere plugin or companion update!${c_default}"
echo ""
echo ""

# Pull the repo to get the top of master.
echo "Pulling the latest version..."
git checkout master > /dev/null 2> /dev/null
git pull --quiet > /dev/null

# Our installer script has all of the logic to update system deps, py deps, and the py environment.
# So we use it with a special flag to do updating.
echo "Running the update..."
./install.sh -update