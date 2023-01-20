import os
import sys

from .moonrakerhost import MoonrakerHost

# sys.argv = [
#     "", # Program Name
#     "/home/pi/printer_data/config", # Moonraker config dir
#     "/home/pi/printer_data/logs",   # Moonraker logs dir
#     "/home/pi/octoeverywhere-storage", # OE local storage dir
#     "octoeverywhere",               # The service name
#     "/home/pi/octoeverywhere-env",  # The root of our py virt env
#     "/home/pi/octoeverywhere"       # The repo root
# ]

if __name__ == '__main__':
    # The config and settings path is passed as the first arg when the service runs.
    # This allows us to run multiple services instances, each pointing at it's own config.
    if len(sys.argv) < 1:
        print("ERROR! - No config and settings path passed to service.")
        sys.exit(1)
    expectedArgCount = 7
    if len(sys.argv) != expectedArgCount:
        print("ERROR! - Missing required startup args. Has: "+str(len(sys.argv)) + " Expected: "+str(expectedArgCount))
        sys.exit(1)

    # The first path is the klipper config folder for this instance KlipperConfigFolder
    klipperConfigFolder = sys.argv[1]
    if os.path.exists(klipperConfigFolder) is False:
        print("ERROR! - KlipperConfigFolder doesn't exist. "+str(klipperConfigFolder))
        sys.exit(1)
    # The second is the klipper log folder
    klipperLogFolder = sys.argv[2]
    if os.path.exists(klipperLogFolder) is False:
        print("ERROR! - KlipperLogFolder doesn't exist. "+str(klipperLogFolder))
        sys.exit(1)
    # The third is our local storage path for this instance.
    localStoragePath = sys.argv[3]
    if os.path.exists(localStoragePath) is False:
        print("ERROR! - LocalFileStoragePath doesn't exist. "+str(localStoragePath))
        sys.exit(1)
    # The fourth is our local storage path for this instance.
    serviceName = sys.argv[4]
    if serviceName is None or len(serviceName) == 0:
        print("ERROR! - serviceName doesn't exist.")
        sys.exit(1)
    # The fifth is our virt env root path for this instance.
    pyVirtEnvRoot = sys.argv[5]
    if os.path.exists(pyVirtEnvRoot) is False:
        print("ERROR! - pyVirtEnvRoot doesn't exist.")
        sys.exit(1)
    # The fifth is our virt env root path for this instance.
    repoRoot = sys.argv[6]
    if os.path.exists(repoRoot) is False:
        print("ERROR! - repoRoot doesn't exist.")
        sys.exit(1)

    # Create and run the main host!
    host = MoonrakerHost(klipperConfigFolder, klipperLogFolder)
    host.RunBlocking(klipperConfigFolder, klipperLogFolder, localStoragePath, serviceName, pyVirtEnvRoot, repoRoot)

    # If we exit here, it's due to an error, since this should be blocked forever.
    sys.exit(1)
