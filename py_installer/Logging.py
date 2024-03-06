import os
import logging
from datetime import datetime
# pylint: disable=import-error # Only exists on linux
import pwd

#
# Output Helpers
#
class BashColors:
    Green='\033[92m'
    Yellow='\033[93m'
    Magenta='\033[0;35m'
    Red="\033[1;31m"
    Cyan="\033[1;36m"
    Default="\033[0;0m"

class Logger:

    IsDebugEnabled = False
    OutputFile = None
    OutputFilePath = None
    PyLogger = None

    @staticmethod
    def InitFile(userHomePath:str, userName:str):
        try:
            Logger.OutputFilePath = os.path.join(userHomePath, "octoeverywhere-installer.log")

            # pylint: disable=consider-using-with
            Logger.OutputFile = open(Logger.OutputFilePath, "w", encoding="utf-8")

            # Ensure the file is permission to the user who ran the script.
            # Note we can't ref Util since it depends on the Logger.
            uid = pwd.getpwnam(userName).pw_uid
            gid = pwd.getpwnam(userName).pw_gid
            # pylint: disable=no-member # Linux only
            os.chown(Logger.OutputFilePath, uid, gid)
        except Exception as e:
            print("Failed to make log file. "+str(e))


    @staticmethod
    def Finalize():
        try:
            Logger.OutputFile.flush()
            Logger.OutputFile.close()
        except Exception:
            pass


    # Returns a logging.Logger standard logger which can be used in the common PY files.
    @staticmethod
    def GetPyLogger() -> logging.Logger:
        if Logger.PyLogger is None:
            Logger.PyLogger = logging.getLogger("octoeverywhere-installer")
            Logger.PyLogger.setLevel(logging.DEBUG)
            Logger.PyLogger.addHandler(CustomLogHandler())
        return Logger.PyLogger


    @staticmethod
    def DeleteLogFile():
        try:
            Logger.Finalize()
            os.remove(Logger.OutputFilePath)
        except Exception:
            pass


    @staticmethod
    def EnableDebugLogging():
        Logger.IsDebugEnabled = True


    @staticmethod
    def Debug(msg) -> None:
        Logger._WriteToFile("Debug", msg)
        if Logger.IsDebugEnabled is True:
            print(BashColors.Yellow+"DEBUG: "+BashColors.Green+msg+BashColors.Default)


    @staticmethod
    def Header(msg)  -> None:
        print(BashColors.Cyan+msg+BashColors.Default)
        Logger._WriteToFile("Info", msg)


    @staticmethod
    def Blank() -> None:
        print("")


    @staticmethod
    def Info(msg) -> None:
        print(BashColors.Green+msg+BashColors.Default)
        Logger._WriteToFile("Info", msg)


    @staticmethod
    def Warn(msg) -> None:
        print(BashColors.Yellow+msg+BashColors.Default)
        Logger._WriteToFile("Warn", msg)


    @staticmethod
    def Error(msg) -> None:
        print(BashColors.Red+msg+BashColors.Default)
        Logger._WriteToFile("Error", msg)


    @staticmethod
    def Purple(msg) -> None:
        print(BashColors.Magenta+msg+BashColors.Default)
        Logger._WriteToFile("Info", msg)


    @staticmethod
    def _WriteToFile(level:str, msg:str):
        try:
            Logger.OutputFile.write(str(datetime.now()) + " ["+level+"] - " + msg+"\n")
        except Exception:
            pass


# Allows us to return a logging.Logger for use in common classes.
class CustomLogHandler(logging.Handler):
    def emit(self, record:logging.LogRecord):
        if record.levelno == logging.DEBUG:
            Logger.Debug(record.getMessage())
        elif record.levelno == logging.INFO:
            Logger.Info(record.getMessage())
        elif record.levelno == logging.WARNING:
            Logger.Warn(record.getMessage())
        elif record.levelno == logging.ERROR:
            Logger.Error(record.getMessage())
        else:
            Logger.Info("Unknown logging level "+record.getMessage())
