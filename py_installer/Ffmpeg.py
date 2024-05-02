import time

from .Util import Util
from .Logging import Logger
from .Context import Context, OsTypes

# A helper class to make sure ffmpeg is installed.
class Ffmpeg:

    # Tries to install ffmpeg, but this won't fail if the install fails.
    @staticmethod
    def TryToInstallFfmpeg(context:Context):

        # We don't even try installing ffmpeg on K1 or SonicPad.
        if context.OsType == OsTypes.K1 or context.OsType == OsTypes.SonicPad:
            return

        # Try to install or upgrade.
        Logger.Info("Installing ffmpeg, this might take a moment...")
        startSec = time.time()
        (returnCode, stdOut, stdError) = Util.RunShellCommand("sudo apt-get install ffmpeg -y", False)

        # Report the status to the installer log.
        Logger.Debug(f"FFmpeg install result. Code: {returnCode}, StdOut: {stdOut}, StdErr: {stdError}")
        if returnCode == 0:
            Logger.Info(f"Ffmpeg successfully installed/updated. It took {str(round(time.time()-startSec, 2))} seconds.")
            return

        # Tell the user, but this is a best effort, so if it fails we don't care.
        # Any user who wants to use RTSP and doesn't have ffmpeg installed can use our help docs to install it.
        Logger.Info(f"We didn't install ffmpeg. It took {str(round(time.time()-startSec, 2))} seconds. Output: {stdError}")
