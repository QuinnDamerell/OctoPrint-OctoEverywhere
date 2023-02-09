#!/bin/bash

#
# The responsibility of this script is to setup the required system libs, environment, and py requirements
# and then passing the rest of install responsibility to the python setup script.
#

# Set this to terminate on error.
set -e

# Get the root path of the repo, aka, where this script is executing
OE_REPO_DIR=$(realpath $(dirname "$0"))

# This is the root of where our py virt env will be
OE_ENV="${HOME}/octoeverywhere-env"

# Note that this is parsed by the update process to find and update required system packages on update!
# On update THIS SCRIPT ISN'T RAN, only this line is parsed out and used to install / update system packages.
# For python packages, the `requirements.txt` package is used on update.
# This var name MUST BE `PKGLIST`!!
PKGLIST="python3 python3-pip python3-venv"

#
# Console Write Helpers
#
c_default=$(echo -en "\e[39m")
c_green=$(echo -en "\e[92m")
c_yellow=$(echo -en "\e[93m")
c_magenta=$(echo -en "\e[35m")
c_red=$(echo -en "\e[91m")
c_cyan=$(echo -en "\e[96m")

log_header()
{
    echo -e "${c_magenta}$1${c_default}"
}

log_important()
{
    echo -e "${c_yellow}$1${c_default}"
}

log_error()
{
    log_blank
    echo -e "${c_red}$1${c_default}"
    log_blank
}

log_info()
{
    echo -e "${c_green}$1${c_default}"
}

log_blank()
{
    echo ""
}

#
# Logic to create / update our virtual py env
#
ensure_py_venv()
{
    log_header "Checking Python Virtual Environment For OctoEverywhere..."
    # If the service is already running, we can't recreate the virtual env
    # so if it exists, don't try to create it.
    if [ -d $OE_ENV ]; then
        log_info "Virtual environment found, updating to the latest version of python."
        python3 -m venv --upgrade "${OE_ENV}"
        return 0
    fi

    log_info "No virtual environment found, creating one now."
    mkdir -p "${OE_ENV}"
    virtualenv -p /usr/bin/python3 --system-site-packages "${OE_ENV}"
}

#
# Logic to make sure all of our required system and PY libs are installed
#
install_or_update_dependencies()
{
    log_header "Checking required system packages are installed..."
    log_important "You might be asked for your system password, this is required for apt-get to install system packages."

    # These we require to be installed in the OS.
    # Note we need to do this before we create our virtual environment
    sudo apt-get update --allow-releaseinfo-change
    sudo apt-get install --yes ${PKGLIST}
    log_info "System package install complete."

    # Now, ensure the virtual environment is created.
    ensure_py_venv

    # Finally, ensure our plugin requirements are installed and updated.
    log_info "Installing or updating required python libs..."
    "${OE_ENV}"/bin/pip3 install -q -r "${OE_REPO_DIR}"/requirements.txt
    log_info "Python libs installed."
}

#
# Logic to ensure the user isn't trying to use this script to setup in OctoPrint.
#
check_for_octoprint()
{
    # Do a basic check to see if OctoPrint is running on the standard port.
    # This obviously doesn't work for all OctoPrint setups, but it works for the default ones.
    if curl -s "http://127.0.0.1:5000" >/dev/null ; then
        log_important "Just a second... OctoPrint was detected!"
        log_blank
        log_important "This install script is used to install OctoEverywhere for Mainsail, Fluidd, Moonraker, etc."
        log_important "If you want to install OctoEverywhere for OctoPrint, you need to use OctoPrint's Plugin Manager, found in OctoPrint's web settings UI."
        log_blank
        read -p       "Do you want to continue this setup for Mainsail, Fluidd, Moonraker, etc? [y/n]: " -e result
        log_blank
        if [ "${result^^}" != "Y" ] ; then
            log_info "Stopping install process."
            exit 0
        fi
    fi
}

log_blank
log_blank
log_blank
cat << EOF
@@@@@@@@@@@@@@@@@@@@@@@@***@@@@@@@@@@@@@@@@@@@@@@@
@@@@@@@@@@@@@@***********************@@@@@@@@@@@@@
@@@@@@@@@@*******************************@@@@@@@@@
@@@@@@@@***********************************@@@@@@@
@@@@@,,,************************/////////*****@@@@
@@@@,,,,,,*****************//////////////******@@@
@@,,,,,,,,,,***********//////////////////*******@@
@@,,,,,,,,,,,,*******////////****///////*********@
@,,,,,,,,,,,/////////////////****//////***********
@,,,,,,,//////////////////////////////************
,,,,,,,,////////////////////////////**************
@,,,,,,,,,,,,/////////////////////****************
@,,,,,,,,,,,,,,/////////////////******************
@@,,,,,,,,,,,,,,,,//////////////*****************@
@@@,,,,,/#######,,,,///////////*****************@@
@@@@,,,##########,,,,,,,//////,****************@@@
@@@@@,##########,,,,,,,,,////,,,,*************@@@@
@@@@@########,,,,,,,,,,,,//,,,,,,,,********@@@@@@@
@@@@@#@@@@,,,,,,,,,,,,,,,,,,,,,,,,,,,***,@@@@@@@@@
@@@@@@@@@@@@@@@,,,,,,,,,,,,,,,,,,,,,@@@@@@@@@@@@@@

           OctoEverywhere For Klipper
EOF
log_blank
log_blank
log_info "OctoEverywhere empowers the worldwide maker community with free Klipper remote access, AI failure detection, notifications, live streaming, and so much more!" 
log_blank
log_blank

# Before we do anything, check that OctoPrint isn't found. If it is, we want to check with the user to make sure they are
# not trying to setup OE for OctoPrint.
check_for_octoprint

# The first thing we need to do is install or updated packages and ensure our virtual environment is setup.
# Since we need to make sure PY is installed, then create the virtual env, then update the PY libs, all of this
# is handled by one function.
install_or_update_dependencies

# Before launching our PY script, set any vars it needs to know
USERNAME=${USER}
USER_HOME=${HOME}
OPTIONAL_MOONRAKER_CONFIG_FILE=${1}
PY_LAUNCH_JSON="{\"OE_REPO_DIR\":\"${OE_REPO_DIR}\",\"OE_ENV\":\"${OE_ENV}\",\"USERNAME\":\"${USERNAME}\",\"USER_HOME\":\"${USER_HOME}\",\"MOONRAKER_CONFIG\":\"${OPTIONAL_MOONRAKER_CONFIG_FILE}\"}"

# Now launch into our py setup script, that does everything else required.
log_info "Running the python install finisher..."
sudo ${OE_ENV}/bin/python3 "${OE_REPO_DIR}/moonraker-install-completer.py" ${PY_LAUNCH_JSON}

# Check the output of the py script.
retVal=$?
if [ $retVal -ne 0 ]; then
    log_error "Failed to complete setup. Error Code: ${retVal}"
fi

# Note the rest of the user flow (and terminal info) is done by the PY script, so we don't need to report anything else.
exit $retVal