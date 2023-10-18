import os

from .Context import Context
from .Logging import Logger
from .Util import Util

class Permissions:
    # Must be lower case.
    c_RootUserName = "root"

    # For some companion setups, users only use one user on the device, root.
    # In this case, it's ok to install as root, but sometimes the $USER is empty
    # Thus, if the home user path is root, we will update the user to be root as well.
    # Note that since the install script always cd ~, we should have the correct home user.
    #
    # Also note, this function runs before the first context validation, so the vars could be null.
    def CheckUserAndCorrectIfRequired_RanBeforeFirstContextValidation(self, context:Context) -> None:
        # If this is a companion install, check if we need to set the user name.
        # It's ok be ran as root, but sometimes the bash USER var isn't set to the user name.
        if context.IsObserverSetup:
            if context.UserName is None or len(context.UserName) == 0:
                # Since the install script does a cd ~, we know if the user home path starts with /root/, the user is root.
                if context.UserHomePath is not None and context.UserHomePath.lower().startswith("/root/"):
                    Logger.Info("No user passed, but we detected the user is root.")
                    context.UserName = Permissions.c_RootUserName


    def EnsureRunningAsRootOrSudo(self, context:Context) -> None:
        # IT'S NOT OK TO INSTALL AS ROOT for the normal klipper setup.
        # This is because the moonraker updater system needs to get able to access the .git repo.
        # If the repo is owned by the root, it can't do that.
        if context.IsObserverSetup is False:
            if context.UserName.lower() == Permissions.c_RootUserName:
                raise Exception("The installer was ran under the root user, this will cause problems with Moonraker. Please run the installer script as a non-root user, usually that's the `pi` user.")

        # But regardless of the user, we must have sudo permissions.
        # pylint: disable=no-member # Linux only
        if os.geteuid() != 0:
            if context.Debug:
                Logger.Warn("Not running as root, but ignoring since we are in debug.")
            else:
                raise Exception("Script not ran as root or using sudo. This is required to integrate into Moonraker.")


    # For all setups, make sure the entire repo is owned by the user who launched the script.
    # This is required, in case the user accidentally used the wrong user at first and some part of the git repo is owned by the root user.
    # If Moonraker is running locally and this is owned by root for example, the Moonraker Updater can't access it, and will show errors.
    def EnsureRepoPermissions(self, context:Context) -> None:
        Logger.Info("Checking git repo permissions...")
        Util.SetFileOwnerRecursive(context.RepoRootFolder, context.UserName)
