#!/bin/bash



#
# OctoEverywhere for Klipper!
#
# This script works for any setup of OctoEverywhere, the normal plugin install, companion install, or a Creality install.
# The script will automatically find all OctoEverywhere instances on this device and update them.
#
# If you need help, feel free to contact us at support@octoeverywhere.com
#



# Set if we are running the Creality OS or not.
# We use the presence of opkg as they key
IS_CREALITY_OS=false
if command -v opkg &> /dev/null
then
    IS_CREALITY_OS=true
fi

c_default=$(echo -en "\e[39m")
c_green=$(echo -en "\e[92m")
c_yellow=$(echo -en "\e[93m")
c_magenta=$(echo -en "\e[35m")
c_red=$(echo -en "\e[91m")
c_cyan=$(echo -en "\e[96m")

echo ""
echo ""
echo -e "${c_yellow}Starting an OctoEverywhere update!${c_default}"
echo ""
echo ""

# Since our cron update script runs as root and git commands have to be ran by the owner,
# when we run the git commands, we need to make sure we are the right user.
runAsRepoOwner()
{
    # For Creality OS, we can't use stat or whoami, but there's only one user anyways, root.
    # So always just run it.
    if $IS_CREALITY_OS
    then
        eval $1
        return
    fi

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
runAsRepoOwner "git fetch --tags"

# Find the latest tag, just for stats now.
# We have to make sure we are on the master branch or the Moonraker updater won't work.
# The Moonraker updater will pull to master on to the latest tag, but we don't do that for now.
latestTaggedCommit=$(runAsRepoOwner "git rev-list --tags --max-count=1")
latestTag=$(runAsRepoOwner "git describe --tags ${latestTaggedCommit}")
currentGitStatus=$(runAsRepoOwner "git describe")
echo "Latest git tag found ${latestTag}, current status ${currentGitStatus}"

# Reset any local changes and pull the head of master.
runAsRepoOwner "git reset --hard --quiet"
runAsRepoOwner "git checkout master --quiet"
runAsRepoOwner "git pull --quiet"

# Our installer script has all of the logic to update system deps, py deps, and the py environment.
# So we use it with a special flag to do updating.
echo "Running the update..."
./install.sh -update
