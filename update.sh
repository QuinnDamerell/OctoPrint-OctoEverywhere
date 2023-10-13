#!/bin/bash

#
# OctoEverywhere for Klipper!
# This script is used to UPDATE OctoEverywhere for Klipper.
# Note this script is ran by the crontab updater, so it's name and location can't be moved!
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

# Since our cron update script runs as root and git commands have to be ran by the owner,
# when we run the git commands, we need to make sure we are the right user.
runAsRepoOwner()
{
    updateScriptOwner=$(stat -c %U update.sh)
    if [[ $(whoami) == *${updateScriptOwner}* ]]; then
        eval $1
    else
        repoDir=$(realpath $(dirname "$0"))
        sudo su - ${updateScriptOwner} -c "cd ${repoDir} && $1"
    fi
}

# Pull the repo to get the top of master.
echo "Updating repo and fetching the latest released tag..."
runAsRepoOwner "git checkout master > /dev/null 2> /dev/null"
runAsRepoOwner "git fetch"

# Find the latest tag, pull to that. We do this so we only get "released" master changes.
latestTag=$(runAsRepoOwner "git describe --abbrev=0 --tags")
currentGitStatus=$(runAsRepoOwner "git describe")
echo "Latest git tag found ${latestTag}, current status ${currentGitStatus}"

# Reset any local changes and pull to the tag.
runAsRepoOwner "git reset --hard --quiet"
runAsRepoOwner "git checkout ${latestTag} --quiet"

# Our installer script has all of the logic to update system deps, py deps, and the py environment.
# So we use it with a special flag to do updating.
echo "Running the update..."
./install.sh -update
