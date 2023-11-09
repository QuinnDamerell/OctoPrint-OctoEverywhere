#!/bin/bash



#
# OctoEverywhere for Klipper!
#
# Use this script to install the plugin on a normal device or a Creality device, or to install the companion!
# For a companion install, use the -companion argument.
#
# Simply run ./install.sh from the git repo root directory to get started!
#
# If you need help, feel free to contact us at support@octoeverywhere.com
#





#
# The responsibility of this script is to bootstrap the setup by installing the required system libs,
# virtual environment, and py requirements. The core of the setup logic is done by the PY install script.
#

# Set this to terminate on error.
set -e

# Set if we are running the Creality OS or not.
# We use the presence of opkg as they key
IS_CREALITY_OS=false
if command -v opkg &> /dev/null
then
    IS_CREALITY_OS=true
    # We install everything at this path, which is a fixed path where moonraker and klipper are also installed.
    HOME="/usr/share"
fi

# Get the root path of the repo, aka, where this script is executing
OE_REPO_DIR=$(readlink -f $(dirname "$0"))

# This is the root of where our py virtual env will be. Note that all OctoEverywhere instances share this same
# virtual environment. This how the rest of the system is, where all other services, even with multiple instances, share the same
# virtual environment. I probably wouldn't have done it like this, but we have to create this before we know what instance we are targeting, so it's fine.
OE_ENV="${HOME}/octoeverywhere-env"

# Note that this is parsed by the update process to find and update required system packages on update!
# On update THIS SCRIPT ISN'T RAN, only this line is parsed out and used to install / update system packages.
# For python packages, the `requirements.txt` package is used on update.
# This var name MUST BE `PKGLIST`!!
#
# The python requirements are for the installer and plugin
# The virtualenv is for our virtual package env we create
# The curl requirement is for some things in this bootstrap script.
PKGLIST="python3 python3-pip virtualenv curl"
# For the Creality OS, we only need to install these.
# We don't override the default name, since that's used by the Moonraker installer
CREALITY_PKGLIST="python3 python3-pip"


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
# It's important for consistency that the repo root is in /usr/share on Creality OS.
# To enforce that, we will move the repo where it should be.
#
ensure_creality_os_right_repo_path()
{
    if $IS_CREALITY_OS
    then
        EXPECT='/usr/share/'
        if [[ "$OE_REPO_DIR" != *"$EXPECT"* ]]; then
            log_error "For the Creality OS this repo must be cloned into /usr/share/octoeverywhere."
            log_important "Moving the repo and running the install again..."
            cd /usr/share
            # Send errors to null, if the folder already exists this will fail.
            git clone https://github.com/QuinnDamerell/OctoPrint-OctoEverywhere octoeverywhere 2>/dev/null || true
            cd /usr/share/octoeverywhere
            # Ensure state
            git reset --hard
            git checkout master
            git pull
            # Run the install, if it fails, still do the clean-up of this repo.
            ./install.sh "$@" || true
            installExit=$?
            # Delete this folder.
            rm -fr $OE_REPO_DIR
            # Take the user back to the new install folder.
            cd /usr/share/
            # Exit.
            exit $installExit
        fi
    fi
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
        # This virtual env refresh fails on some devices when the service is already running, so skip it for now.
        # This only refreshes the virtual environment package anyways, so it's not super needed.
        #log_info "Virtual environment found, updating to the latest version of python."
        #python3 -m venv --upgrade "${OE_ENV}"
        return 0
    fi

    log_info "No virtual environment found, creating one now."
    mkdir -p "${OE_ENV}"
    virtualenv -p /usr/bin/python3 --system-site-packages "${OE_ENV}"
}

#
# Logic to make sure all of our required system packages are installed.
#
install_or_update_system_dependencies()
{
    log_header "Checking required system packages are installed..."

    if $IS_CREALITY_OS
    then
        # On the Creality OS, we only need to run these installers
        opkg install ${CREALITY_PKGLIST}
        pip3 install virtualenv
    else
        log_important "You might be asked for your system password - this is required to install the required system packages."

        # It seems a lot of printer control systems don't have the date and time set correctly, and then the fail
        # getting packages and other downstream things. We will will use our HTTP API to set the current UTC time.
        # Note that since cloudflare will auto force http -> https, we use https, but ignore cert errors, that could be
        # caused by an incorrect date.
        # Note some companion systems don't have curl installed, so this will fail.
        sudo date -s `curl --insecure 'https://octoeverywhere.com/api/util/date' 2>/dev/null` || true

        # These we require to be installed in the OS.
        # Note we need to do this before we create our virtual environment
        sudo apt update
        sudo apt install --yes ${PKGLIST}

        # The PY lib Pillow depends on some system packages that change names depending on the OS.
        # The easiest way to do this was just to try to install them and ignore errors.
        # Most systems already have the packages installed, so this only fixes edge cases.
        # Notes on Pillow deps: https://pillow.readthedocs.io/en/latest/installation.html
        log_info "Ensuring zlib is install for Pillow, it's ok if this package install fails."
        sudo apt install --yes zlib1g-dev 2> /dev/null || true
        sudo apt install --yes zlib-devel 2> /dev/null || true
    fi

    log_info "System package install complete."
}

#
# Logic to install or update the virtual env and all of our required packages.
#
install_or_update_python_env()
{
    # Now, ensure the virtual environment is created.
    ensure_py_venv

    # Update pip if needed - we added a note because this takes a while on the sonic pad.
    log_info "Updating PIP if needed... (this can take a few seconds or so)"
    "${OE_ENV}"/bin/python -m pip install --upgrade pip

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
    if $IS_CREALITY_OS
    then
        # Skip, there's no need and we don't have curl.
        return
    else
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
EOF
log_blank
log_header "           OctoEverywhere For Klipper"
log_blank
log_blank
log_important "OctoEverywhere empowers the worldwide maker community with..."
log_info      "  - Free & Unlimited Mainsail and Fluidd Remote Access"
log_info      "  - Free & Unlimited Next-Gen AI Print Failure Detection"
log_info      "  - Real-Time Print Notifications"
log_info      "  - And So Much More"
log_blank
log_blank

if $IS_CREALITY_OS
then
    echo "Running in Creality OS mode"
fi

# Before anything, make sure this repo is cloned into the correct path on Creality OS devices.
# If this is Creality OS and the path is wrong, it will re-clone the repo, run the install again, and exit.
ensure_creality_os_right_repo_path

# Next, make sure our required system packages are installed.
# These are required for other actions in this script, so it must be done first.
install_or_update_system_dependencies

# Check that OctoPrint isn't found. If it is, we want to check with the user to make sure they are
# not trying to setup OE for OctoPrint.
check_for_octoprint

# Now make sure the virtual env exists, is updated, and all of our currently required PY packages are updated.
install_or_update_python_env

# Before launching our PY script, set any vars it needs to know
# Pass all of the command line args, so they can be handled by the PY script.
# Note that USER can be empty string on some systems when running as root. This is fixed in the PY installer.
USERNAME=${USER}
USER_HOME=${HOME}
CMD_LINE_ARGS=${@}
PY_LAUNCH_JSON="{\"OE_REPO_DIR\":\"${OE_REPO_DIR}\",\"OE_ENV\":\"${OE_ENV}\",\"USERNAME\":\"${USERNAME}\",\"USER_HOME\":\"${USER_HOME}\",\"CMD_LINE_ARGS\":\"${CMD_LINE_ARGS}\"}"
log_info "Bootstrap done. Starting python installer..."

# Now launch into our py setup script, that does everything else required.
# Since we use a module for file includes, we need to set the path to the root of the module
# so python will find it.
export PYTHONPATH="${OE_REPO_DIR}"

# We can't use pushd on Creality OS, so do this.
CURRENT_DIR=${pwd}
cd ${OE_REPO_DIR} > /dev/null

# Disable the PY cache files (-B), since they will be written as sudo, since that's what we launch the PY
# installer as. The PY installer must be sudo to write the service files, but we don't want the
# complied files to stay in the repo with sudo permissions.
if $IS_CREALITY_OS
then
    # Creality OS only has a root user and we can't use sudo.
    ${OE_ENV}/bin/python3 -B -m moonraker_installer ${PY_LAUNCH_JSON}
else
    sudo ${OE_ENV}/bin/python3 -B -m moonraker_installer ${PY_LAUNCH_JSON}
fi

cd ${CURRENT_DIR} > /dev/null

# Check the output of the py script.
retVal=$?
if [ $retVal -ne 0 ]; then
    log_error "Failed to complete setup. Error Code: ${retVal}"
fi

# Note the rest of the user flow (and terminal info) is done by the PY script, so we don't need to report anything else.
exit $retVal