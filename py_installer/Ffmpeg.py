import time

from .Util import Util
from .Logging import Logger

# A helper class to make sure ffmpeg is installed.
class Ffmpeg:

    # Tries to install ffmpeg, but this won't fail if the install fails.
    @staticmethod
    def EnsureFfmpeg():
        # Try to install or upgrade.
        Logger.Info("Installing ffmpeg, this might take a moment...")
        startSec = time.time()
        (returnCode, stdOut, stdError) = Util.RunShellCommand("sudo apt-get install ffmpeg -y", False)

        # Report the status to the installer log.
        Logger.Debug(f"FFmpeg install result. Code: {returnCode}, StdOut: {stdOut}, StdErr: {stdError}")
        if returnCode == 0:
            Logger.Info(f"Ffmpeg successfully installed/updated. It took {str(round(time.time()-startSec, 2))} seconds.")
            return

        # Warn, but don't throw or stop the installer.
        Logger.Warn(f"Ffmpeg failed to install. It took {str(round(time.time()-startSec, 2))} seconds. Error: {stdError}")
