from .Util import Util
from .Logging import Logger

from .Context import Context

# This helper class ensures that the system's ntp clock sync service is enabled and active.
# We found some MKS PI systems didn't have it on, and would be years out of sync on reboot.
# This is a problem because SSL will fail if the date is too far out of sync.
#
# For the most part, this class is best effort. It will try to get everything setup, but if it fails,
# we won't stop the setup.
class TimeSync:

    @staticmethod
    def EnsureNtpSyncEnabled(context:Context):
        if context.SkipSudoActions:
            Logger.Warn("Skipping time sync since we are skipping sudo actions.")
            return
        # Don't bother with Creality OS, since it doesn't have sudo nor systemd.
        if context.IsCrealityOs():
            Logger.Debug("Skipping time sync since we are running on a Creality OS.")
            return

        Logger.Info("Ensuring that time sync is enabled...")

        # Ensure that NTP is uninstalled, since this conflicts with timesyncd
        TimeSync._RunSystemCommand("sudo apt -y purge ntp ntpdate ntpsec-ntpdate")

        # Ensure timedatectl is installed. On all most systems it will be already.
        TimeSync._RunSystemCommand("sudo apt install -y systemd-timesyncd")
        TimeSync._PrintTimeSyncDStatus()

        # Ensure time servers are set in the config file.
        TimeSync._UpdateTimeSyncdConfig()

        # Reload and start the systemd service
        TimeSync._RunSystemCommand("sudo systemctl daemon-reload")
        TimeSync._RunSystemCommand("sudo systemctl enable systemd-timesyncd")
        TimeSync._RunSystemCommand("sudo systemctl restart systemd-timesyncd")
        TimeSync._RunSystemCommand("sudo timedatectl set-ntp on")

        # Print the status outcome.
        TimeSync._PrintTimeSyncDStatus()


    @staticmethod
    def _UpdateTimeSyncdConfig():
        targetFilePath = "/etc/systemd/timesyncd.conf"
        try:
            # After writing, read the file and insert any comments we have.
            outputLines = []
            with open(targetFilePath, 'r', encoding="utf-8") as f:
                lines = f.readlines()
                for line in lines:
                    lineLower = line.lower()
                    if lineLower.startswith("#ntp="):
                        # This is case sensitive!
                        outputLines.append("NTP=0.pool.ntp.org 1.pool.ntp.org 2.pool.ntp.org 3.pool.ntp.org\n")
                    else:
                        outputLines.append(line)
            # This will only happen if we have sudo powers.
            with open(targetFilePath, 'w', encoding="utf-8") as f:
                f.writelines(outputLines)
        except Exception as e:
            Logger.Debug(f"TimeSync update config exception. (this is ok) {str(e)}")


    @staticmethod
    def _RunSystemCommand(cmd:str):
        (code, stdOut, errOut) = Util.RunShellCommand(cmd, False)
        if code == 0:
            Logger.Debug(f"TimeSync System Command Success. Cmd: {cmd}")
        if code != 0:
            Logger.Debug(f"TimeSync System Command FAILED. (this is ok) Cmd: `{cmd}` - `{str(stdOut)}` - `{str(errOut)}`")


    @staticmethod
    def _PrintTimeSyncDStatus():
        (_, stdOut, errOut) = Util.RunShellCommand("sudo timedatectl status", False)
        Logger.Debug(f"TimeSync Status:\r\n{stdOut} {errOut}")
