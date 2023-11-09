import os
import subprocess
import platform

from .Proto import OsType


class OsTypeIdentifier:

    #
    # Note! All of this logic and vars should stay in sync with the Moonraker installer OsType logic in Context.py
    #

    # For the Sonic Pad, this is the path we know we will find the printer configs and printer log locations.
    # The printer data will not be standard setup, so it will be like <root folder>/printer_config, <root folder>/printer_logs
    CrealityOsUserDataPath_SonicPad = "/mnt/UDISK"

    # For the K1/K1Max, this is the path we know we will find the printer configs and printer log locations.
    # They will be the standard Klipper setup, such as printer_data/config, printer_data/logs, etc.
    CrealityOsUserDataPath_K1 = "/usr/data"

    @staticmethod
    def DetectOsType() -> OsType:
        # Do a quick check for windows first.
        # This is only possible on OctoPrint right now.
        if platform.system().lower == "windows":
            return OsType.OsType.Windows

        # We use the presence of opkg to figure out if we are running no Creality OS
        # This is the same thing we do in the installer and update scripts.
        result = subprocess.run("command -v opkg", check=False, shell=True, capture_output=True, text=True)
        if result.returncode == 0:
            # This is a Creality OS.
            # Now we need to detect if it's a Sonic Pad or a K1
            if os.path.exists(OsTypeIdentifier.CrealityOsUserDataPath_SonicPad):
                # Note that this type implies that the system can't self update.
                return OsType.OsType.CrealitySonicPad
            if os.path.exists(OsTypeIdentifier.CrealityOsUserDataPath_K1):
                # Note that this type implies that the system can't self update.
                return OsType.OsType.CrealityK1
            return OsType.OsType.Unknown

        # The OS is debian
        return OsType.OsType.Debian
