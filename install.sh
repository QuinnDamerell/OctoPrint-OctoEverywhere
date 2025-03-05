#!/bin/bash

#
# OctoEverywhere for Klipper, Creality, Bambu Lab, Elegoo, and other 3D Printers!
#
# Use this script to install the OctoEverywhere plugin for:
#    OctoEverywhere for Klipper    - The plugin is connected to Moonraker running on this device.
#    OctoEverywhere for Creality   - The plugin is being installed on a Creality device (Sonic Pad, K1, etc)
#    OctoEverywhere Companion      - The plugin will connect to Moonraker running on a different device on the same LAN
#    OctoEverywhere Bambu Connect  - The plugin will connect to A Bambu Lab printer running on the save LAN.
#    OctoEverywhere Elegoo Connect - The plugin will connect to A Elegoo printer running on the save LAN.
#
# For local Klipper or Creality devices, no arguments are required.
# For a companion install, use the -companion argument.
# For a Bambu Connect install, use the -bambu argument.
# For a Elegoo Connect install, use the -elegoo argument.
#
# Simply run ./install.sh from the git repo root directory to get started!
#
# If you need help, feel free to contact us at support@octoeverywhere.com
#





#
# The responsibility of this script is to bootstrap the setup by installing the required system libs,
# virtual environment, and py requirements. The core of the setup logic is done by the PY install script.
#

# We don't do this anymore, because some commands return non-zero exit codes, but still are successful.
# set -e

#
# First things first, we need to detect what kind of OS we are running on. The script works by default with all
# Debian OSs, but some printers with embedded computers run strange embedded OSs, that have a lot of restrictions.
# These must stay in sync with update.sh and uninstall.sh!
#

# The K1 and K1 Max run an OS called Buildroot. We detect that by looking at the os-release file.
# Quick note about bash vars, to support all OSs, we use the most compilable var version. This means we use ints
# where 1 is true and 0 is false, and we use comparisons like this [[ $IS_K1_OS -eq 1 ]]
IS_K1_OS=0
if grep -Fqs "ID=buildroot" /etc/os-release
then
    IS_K1_OS=1
    # On the K1, we always want the path to be /usr/data
    # /usr/share has very limited space, so we don't want to use it.
    # This is also where the github script installs moonraker and everything.
    HOME="/usr/data"
fi

# Next, we try to detect if this OS is the Sonic Pad OS.
# The Sonic Pad runs openwrt. We detect that by looking at the os-release file.
IS_SONIC_PAD_OS=0
if grep -Fqs "sonic" /etc/openwrt_release
then
    IS_SONIC_PAD_OS=1
    # On the K1, we always want the path to be /usr/share, this is where the rest of the klipper stuff is.
    HOME="/usr/share"
fi

# Next, we try to detect if this OS is the K2 Plus.
# The K2 runs an openwrt distro called Tina. We detect that by looking at the openwrt_release file.
IS_K2_OS=0
if grep -Fiqs "tina" /etc/openwrt_release
then
    IS_K2_OS=1
    # On the K2, we always want the path to be /mnt/UDISK, since it has a lot of space there.
    # The default moonraker instance is installed in /usr/share/moonraker/
    HOME="/mnt/UDISK"
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
# Note! This was deprecated in newer versions of moonraker, instead the deps are in the moonraker-system-dependencies.json file.
# For now we will keep both around AND IN SYNC so we can support older versions of moonraker.
#
# The python requirements are for the installer and plugin
# The virtualenv is for our virtual package env we create
# The curl requirement is for some things in this bootstrap script.
# python3-venv is required for teh virtualenv command to fully work.
# This must stay in sync with the dockerfile package installs
PKGLIST="python3 python3-pip virtualenv python3-venv curl"
# For the Creality OS, we only need to install these.
# We don't override the default name, since that's used by the Moonraker installer
# Note that we DON'T want to use the same name as above (not even in this comment) because some parsers might find it.
# Note we exclude virtualenv python3-venv curl because they can't be installed on the sonic pad via the package manager.
CREALITY_DEP_LIST="python3 python3-pip python3-pillow"
SONIC_PAD_DEP_LIST="python3 python3-pip"

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

log_blue()
{
    echo -e "${c_cyan}$1${c_default}"
}

log_blank()
{
    echo ""
}

#
# It's important for consistency that the repo root is in set $HOME for the K1, K2, and Sonic Pad
# To enforce that, we will move the repo where it should be.
ensure_creality_os_right_repo_path()
{
    # TODO - re-enable this for the  || [[ $IS_K1_OS -eq 1 ]] after the github script updates.
    if [[ $IS_SONIC_PAD_OS -eq 1 ]] || [[ $IS_K2_OS -eq 1 ]]
    then
        # Due to the K1 shell, we have to use grep rather than any bash string contains syntax.
        if echo $OE_REPO_DIR |grep "$HOME" - > /dev/null
        then
            return
        else
            log_info "Current path $OE_REPO_DIR"
            log_error "For the Creality devices the OctoEverywhere repo must be cloned into $HOME/octoeverywhere"
            log_important "Moving the repo and running the install again..."
            cd $HOME
            # Send errors to null, if the folder already exists this will fail.
            git clone https://github.com/QuinnDamerell/OctoPrint-OctoEverywhere octoeverywhere 2>/dev/null || true
            cd $HOME/octoeverywhere
            # Ensure state
            git reset --hard
            git checkout master
            git pull
            # Run the install, if it fails, still do the clean-up of this repo.
            if [[ $IS_K1_OS -eq 1 ]]
            then
                sh ./install.sh "$@" || true
            else
                ./install.sh "$@" || true
            fi
            installExit=$?
            # Delete this folder.
            rm -fr $OE_REPO_DIR
            # Take the user back to the new install folder.
            cd $HOME
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
    # If the service is already running, we can't recreate the virtual env so if it exists, don't try to create it.
    # Note that we check the bin folder exists in the path, since we mkdir the folder below but virtualenv might fail and leave it empty.
    OE_ENV_BIN_PATH="$OE_ENV/bin"
    if [ -d $OE_ENV_BIN_PATH ]; then
        # This virtual env refresh fails on some devices when the service is already running, so skip it for now.
        # This only refreshes the virtual environment package anyways, so it's not super needed.
        #log_info "Virtual environment found, updating to the latest version of python."
        #python3 -m venv --upgrade "${OE_ENV}"
        return 0
    fi

    log_info "No virtual environment found, creating one now."
    mkdir -p "${OE_ENV}"
    if [[ $IS_K1_OS -eq 1 ]] || [[ $IS_K2_OS -eq 1 ]]
    then
        # The K1 requires we setup the virtualenv like this.
        # --system-site-packages is important for the K1, since it doesn't have much disk space.
        # Ideally we use /opt/bin/python3, since that version of python will be updated over time.
        # It installs with the opkg command, if opkg is there.
        # If not, we will use the version of python built into the system for the existing Creality stuff.
        if [[ -f /opt/bin/python3 ]]
        then
            /opt/bin/virtualenv -p /opt/bin/python3 --system-site-packages "${OE_ENV}"
        else
            python3 /usr/lib/python3.8/site-packages/virtualenv.py -p /usr/bin/python3 --system-site-packages "${OE_ENV}"
        fi
    else
        # Everything else can use this more modern style command.
        # We don't want to use --system-site-packages, so we don't consume whatever packages are on the system.
        virtualenv -p /usr/bin/python3 "${OE_ENV}"
    fi
}

#
# Logic to make sure all of our required system packages are installed.
#
install_or_update_system_dependencies()
{
    log_header "Checking required system packages are installed..."

    if [[ $IS_K1_OS -eq 1 ]]
    then
        # The K1 by default doesn't have any package manager. In some cases
        # the user might install opkg via the 3rd party moonraker installer script.
        # But in general, PY will already be installed.
        # We will try to update python from the package manager if possible, otherwise, we will ignore it.
        if [[ -f /opt/bin/opkg ]]
        then
            # Use the full path to ensure it's found, since it might not be in the path if you user didn't restart the printer.
            /opt/bin/opkg update || true
            /opt/bin/opkg install ${CREALITY_DEP_LIST} || true
        fi
        # On the K1, the only we thing we ensure is that virtualenv is installed via pip.
        # We have had users report issues where this install gets stuck, using the no cache dir flag seems to fix it.
        # 5/14/24 - The trusted hosts had to be added to fix a cert issue with pypi we aren't sure why it started happening all of the sudden.
        pip3 install -q --trusted-host pypi.python.org --trusted-host pypi.org --trusted-host=files.pythonhosted.org --no-cache-dir virtualenv
    elif [[ $IS_K2_OS -eq 1 ]]
    then
        # The K2 by default doesn't have any package manager. In some cases
        # the user might install opkg via the 3rd party k2-improvements entware installer.
        # But in general, PY will already be installed.
        # We will try to update python from the package manager if possible, otherwise, we will ignore it.
        if [[ -f /opt/bin/opkg ]]
        then
            # Use the full path to ensure it's found, since it might not be in the path if you user didn't restart the printer.
            /opt/bin/opkg update || true
            /opt/bin/opkg install ${CREALITY_DEP_LIST} || true
        fi
        # On the K2, the only we thing we ensure is that virtualenv is installed via pip.
        pip3 install -q --no-cache-dir virtualenv
    elif [[ $IS_SONIC_PAD_OS -eq 1 ]]
    then
        # The sonic pad always has opkg installed, so we can make sure these packages are installed.
        # We have had users report issues where this install gets stuck, using the no cache dir flag seems to fix it.
        opkg update || true
        opkg install ${SONIC_PAD_DEP_LIST} || true
        pip3 install -q --no-cache-dir virtualenv
    else
        # Print this before the date update, since it might prompt for the user's password.
        log_info "Installing required system packages..."
        log_important "You might be asked for your system password - this is required to install the required system packages."

        # It seems a lot of printer control systems don't have the date and time set correctly, and then the fail
        # getting packages and other downstream things. We will will use our HTTP API to set the current UTC time.
        # Note that since cloudflare will auto force http -> https, we use https, but ignore cert errors, that could be
        # caused by an incorrect date.
        # Note some companion systems don't have curl installed, so this will fail.
        #log_info "Ensuring the system date and time is correct..."
        sudo date -s `curl --insecure 'https://octoeverywhere.com/api/util/date' 2>/dev/null` || true

        # These we require to be installed in the OS.
        # Note we need to do this before we create our virtual environment
        sudo apt update 1>/dev/null 2>/dev/null || true
        sudo apt install --yes ${PKGLIST} 2>/dev/null

        # The PY lib Pillow depends on some system packages that change names depending on the OS.
        # The easiest way to do this was just to try to install them and ignore errors.
        # Most systems already have the packages installed, so this only fixes edge cases.
        # Notes on Pillow deps: https://pillow.readthedocs.io/en/latest/installation.html
        log_info "Ensuring zlib is install for Pillow, it's ok if this package install fails."
        sudo apt install --yes zlib1g-dev 2>/dev/null || true
        sudo apt install --yes zlib-devel 2>/dev/null || true
        sudo apt install --yes python-imaging 2>/dev/null || true
        sudo apt install --yes python3-pil 2>/dev/null || true
        sudo apt install --yes python3-pillow 2>/dev/null || true
    fi

    #log_info "System package install complete."
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
    if [[ $IS_K1_OS -eq 1 ]]
    then
        "${OE_ENV}"/bin/python -m pip install --trusted-host pypi.python.org --trusted-host pypi.org --trusted-host=files.pythonhosted.org --no-cache-dir --upgrade pip
    else
        "${OE_ENV}"/bin/python -m pip install --upgrade pip
    fi

    # Finally, ensure our plugin requirements are installed and updated.
    log_info "Installing or updating required python libs..."
    if [[ $IS_K1_OS -eq 1 ]]
    then
        # The K1 needs some special flags.
        "${OE_ENV}"/bin/pip3 install --trusted-host pypi.python.org --trusted-host pypi.org --trusted-host=files.pythonhosted.org --require-virtualenv --no-cache-dir -q -r "${OE_REPO_DIR}"/requirements.txt
    elif [[ $IS_SONIC_PAD_OS -eq 1 ]]
    then
        # The sonic pad as different requirements, so it doesn't hold back the rest of the installs.
        "${OE_ENV}"/bin/pip3 install --require-virtualenv --no-cache-dir -q -r "${OE_REPO_DIR}"/requirements-sonicpad.txt
    else
        # This is the default for all other systems.
        "${OE_ENV}"/bin/pip3 install --require-virtualenv --no-cache-dir -q -r "${OE_REPO_DIR}"/requirements.txt
    fi
    #log_info "Python libs installed."
}

#
# Logic to ensure the user isn't trying to use this script to setup in OctoPrint.
#
check_for_octoprint()
{
    if [[ $IS_SONIC_PAD_OS -eq 1 ]] || [[ $IS_K1_OS -eq 1 ]] || [[ $IS_K2_OS -eq 1 ]]
    then
        # Skip, there's no need and we don't have curl.
        return
    else
        # Check if we are running in the Bambu Connect or Companion mode, if so, don't do this since
        # The device could be running OctoPrint and that's fine.
        if [[ "$*" == *"-bambu"* ]] || [[ "$*" == *"-companion"* ]]
        then
            return
        fi

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
                         ==============
                     ======================
                   ==========================
                 ==============================
               ==================================
              ======================+#%@@@@*======
             ====================+%@@@@@@@@========
            ===================*@@@%%@@@@@%=========
            =================*@@@@====%@@@==========
            ===========*@@@@@@@@@@====@@@===========
            =========@@@@@@@@@@@@@@@@@@%============
            ============@@@@@@@@@@@@@@+=============
            ============+@@@@@@@@@@@*===============
            ===========%%=#@@@@@@@@@================
            =========+@@@@#=%@@@@@@@================
             ========%@@@@@%===%@@@===============-
              =======@@@@%+=====@@+=============-=
               =================+============-==-
                 =======================-=-=-=-
                   =================-=-=-=---
                     =========-=-=-=-------
                         -==-==-=------
EOF
log_blank
log_header    "  OctoEverywhere For Klipper, Creality, Elegoo, And Bambu Lab Printers"
log_blue      "   The 3D Printing Communities #1 Remote Access And AI Cloud Service"
log_blank
log_blank
log_important "OctoEverywhere empowers the worldwide maker community with..."
log_info      "  - Free & Unlimited Mainsail, Fluidd, Elegoo, Creality, And Bambu Lab Printers Remote Access"
log_info      "  - Free & Unlimited Next-Gen AI Print Failure Detection"
log_info      "  - Free Full Frame Rate & Full Resolution Webcam Streaming"
log_info      "  - 5 Star Rated iOS & Android Apps"
log_info      "  - Real-Time Print Notifications"
log_info      "  - And So Much More"
log_blank
log_blank

# These are helpful for debugging.
if [[ $IS_SONIC_PAD_OS -eq 1 ]]
then
    echo "Running in Sonic Pad OS mode"
fi
if [[ $IS_K1_OS -eq 1 ]]
then
    echo "Running in K1 and K1 Max OS mode"
fi
if [[ $IS_K2_OS -eq 1 ]]
then
    echo "Running in K2 OS mode"
fi

# Before anything, make sure this repo is cloned into the correct path on Creality OS devices.
# If this is Creality OS and the path is wrong, it will re-clone the repo, run the install again, and exit.
ensure_creality_os_right_repo_path

# Next, make sure our required system packages are installed.
# These are required for other actions in this script, so it must be done first.
install_or_update_system_dependencies

# Check that OctoPrint isn't found. If it is, we want to check with the user to make sure they are
# not trying to setup OE for OctoPrint.
check_for_octoprint $*

# Now make sure the virtual env exists, is updated, and all of our currently required PY packages are updated.
install_or_update_python_env

# Before launching our PY script, set any vars it needs to know
# Pass all of the command line args, so they can be handled by the PY script.
# Note that USER can be empty string on some systems when running as root. This is fixed in the PY installer.
USERNAME=${USER}
USER_HOME=${HOME}
CMD_LINE_ARGS=${@}
PY_LAUNCH_JSON="{\"OE_REPO_DIR\":\"${OE_REPO_DIR}\",\"OE_ENV\":\"${OE_ENV}\",\"USERNAME\":\"${USERNAME}\",\"USER_HOME\":\"${USER_HOME}\",\"CMD_LINE_ARGS\":\"${CMD_LINE_ARGS}\"}"
#log_info "Bootstrap done. Starting python installer..."

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
if [[ $IS_SONIC_PAD_OS -eq 1 ]] || [[ $IS_K1_OS -eq 1 ]] || [[ $IS_K2_OS -eq 1 ]]
then
    # Creality OS only has a root user and we can't use sudo.
    ${OE_ENV}/bin/python3 -B -m py_installer ${PY_LAUNCH_JSON}
else
    sudo ${OE_ENV}/bin/python3 -B -m py_installer ${PY_LAUNCH_JSON}
fi

cd ${CURRENT_DIR} > /dev/null

# Check the output of the py script.
retVal=$?
if [ $retVal -ne 0 ]; then
    log_error "Failed to complete setup. Error Code: ${retVal}"
fi

# Note the rest of the user flow (and terminal info) is done by the PY script, so we don't need to report anything else.
exit $retVal
