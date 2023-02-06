import time

from octoeverywhere.compat import Compat

#
# The point of this class is to allow pause commands (from Gadget and else where) that are less likely to cause harm to the current print.
# This is done by moving the hotend back from the print and even cooling it down. This protects the print from heat harm and also prevents the filament
# in the hot end from getting harmed.
#
class SmartPause:

    # The static instance.
    _Instance = None

    @staticmethod
    def Init(logger, octoPrintPrinterObj, octoPrintPrinterProfileObj):
        SmartPause._Instance = SmartPause(logger, octoPrintPrinterObj, octoPrintPrinterProfileObj)
        Compat.SetSmartPauseInterface(SmartPause._Instance)


    @staticmethod
    def Get():
        return SmartPause._Instance


    def __init__(self, logger, octoPrintPrinterObj, octoPrintPrinterProfileObj):
        self.Logger = logger
        self.OctoPrintPrinterObj = octoPrintPrinterObj
        self.OctoPrintPrinterProfileObj = octoPrintPrinterProfileObj

        # Indicates if there is a pause notification suppression.
        self.LastPauseNotificationSuppressionTimeSec = None

        # Keeps track of the last position mode commands we saw in the gcode.
        # Default to the common modes.
        self.LastG9Command = "G90"
        self.LastM8Command = "M82"

        # Used to hold any pause or resume scripts that should be applied when we get
        # the hook for pause and resume. Since that hook fires whenever the printer does a pause
        # and resume, these will stay empty until we do our smart pause and resume.
        self.PauseScripts = []
        self.ResumeScripts = []
        self._ResetScripts()


    # !! Interface Function !! - See compat.py GetSmartPauseInterface for the details.
    # Returns None if there is no current suppression or the time of the last time it was requested
    def GetAndResetLastPauseNotificationSuppressionTimeSec(self):
        local = self.LastPauseNotificationSuppressionTimeSec
        self.LastPauseNotificationSuppressionTimeSec = None
        return local


    # Sets the suppress time to now.
    def SetLastPauseNotificationSuppressionTimeNow(self):
        self.Logger.info("Setting pause time to suppress the pause notification.")
        self.LastPauseNotificationSuppressionTimeSec = time.time()


    # Set up the scripts and executes a pause!
    def DoSmartPause(self, disableHotendBool, disableBedBool, zLiftMm, retractFilamentMm, suppressNotificationBool):
        # Clear any existing script
        self._ResetScripts()

        # Check for the debug setup.
        if self.OctoPrintPrinterObj is None or self.OctoPrintPrinterProfileObj is None:
            self.Logger.warn("Smart Pause doesn't have required OctoPrint objects")
            return

        # Ensure we are printing
        if self.OctoPrintPrinterObj.is_printing() is False:
            self.Logger.warn("Smart Pause tried to pause but the OctoPrint is not in the printing state.")
            return

        # Marlin command guide:
        # https://marlinfw.org/docs/gcode/M140.html

        # Order matters here. For pause, move first then cool. For resume warm first, then move.
        # Start with movement.
        doRetract = retractFilamentMm is not None and retractFilamentMm > 0
        doZLift = zLiftMm is not None and zLiftMm > 0
        if doRetract or doZLift:
            #
            # Create the Pause Script.
            #
            # Put the mode in relative position
            self.PauseScripts.extend(["G91", "M83"])
            # Do the retract first, before the z lift, so we don't ooze as we have seen in testing.
            if doRetract:
                # G1 is the move command, E is the extruder, - to backwards, and the amount
                self.PauseScripts.append("G1 E-"+str(retractFilamentMm))
            # Do the z lift next
            if doZLift:
                # G1 is the move command, z is the axis, the number is the amount.
                self.PauseScripts.append("G1 Z"+str(zLiftMm))
            # Restore the position mode that was being used.
            self.PauseScripts.extend([self.LastG9Command, self.LastM8Command])

            #
            # Create the Resume Script.
            #
            # Put the mode in relative position
            # Remember that if temps are changed, their scripts will be injected before these, to ensure
            # everything is warmed up before we move and extrude.
            self.ResumeScripts.extend(["G91", "M83"])
            # Do the z drop. (opposite of pause ordering)
            if doZLift:
                # G1 is the move command, z is the axis, - to move down, the number is the amount.
                self.ResumeScripts.append("G1 Z-"+str(zLiftMm))
            # Do the extrude last, which will put the filament back in the nozzle as close to the print starting as we can.
            if doRetract:
                # G1 is the move command, E is the extruder, and the amount
                self.ResumeScripts.append("G1 E"+str(retractFilamentMm))
            # Restore the position mode that was being used.
            self.ResumeScripts.extend([self.LastG9Command, self.LastM8Command])

        # Next, do temps.
        # Start the the hotend, order matters once again.
        # If we are doing both the hotend and bed, we want the bed to re-enable first, to ensure the print sticks.
        # We also need to make sure the cooling is done after the retraction.
        currentTemps = self.OctoPrintPrinterObj.get_current_temperatures()
        if disableHotendBool:
            # https://docs.octoprint.org/en/master/modules/printer.html#octoprint.printer.profile.PrinterProfileManager
            extruder = self.OctoPrintPrinterProfileObj.get("extruder")
            count = extruder.get("count", 1)

            # If there is a shared nozzle, only work with the first tool, if there is one.
            if count > 0 and extruder.get("sharedNozzle", False) is True:
                count = 1

            # For each tool, set it up to cool down and then resume the temp.
            # range will make a count starting at 0, excluding the value passed.
            for toolNum in range(count):
                toolId = "tool"+str(toolNum)
                # Make sure we have a current temp target for the tool.
                if toolId in currentTemps and currentTemps[toolId]["target"] is not None and currentTemps[toolId]["offset"] is not None:
                    # Capture the target as well as any offsets.
                    currentTarget = currentTemps[toolId]["target"] + currentTemps[toolId]["offset"]

                    # Add the command to cool down at the end of the current pause list.
                    self.PauseScripts.append("M104 T"+str(toolNum)+" S0")

                    # This command needs to be inserted in the front of the list for resume, so it happens before movement.
                    self.ResumeScripts.insert(0, "M109 T"+str(toolNum)+" S"+str(currentTarget))

        # Handle the bed temp.
        if disableBedBool:
            tool = "bed"
            if tool in currentTemps and currentTemps[tool]["target"] is not None and currentTemps[tool]["offset"] is not None:
                # Capture the target as well as any offsets.
                currentTarget = currentTemps[tool]["target"] + currentTemps[tool]["offset"]
                # Add the command to cool the bed.
                self.PauseScripts.append("M140 S0")
                # Add the command to heat the bed to the start of the resume script.
                self.ResumeScripts.insert(0, "M190 S"+str(currentTarget))

        # If we are suppressing the notification, set the current time, so we know when we last suppressed.
        # We need to do this before the pause command, so the notification system can get the value on the pause action.
        if suppressNotificationBool is True:
            self.SetLastPauseNotificationSuppressionTimeNow()

        # Now call pause on OctoPrint! This pause command will make the OnScriptHook fire, which will consume these scripts we made.
        self.Logger.info("Smart Print scripts created, calling pause on OctoPrint.")
        self.OctoPrintPrinterObj.pause_print()


    def OnScriptHook(self, script_type, script_name):
        # We use OctoPrint's scripts system to dynamically insert commands on the pause and resume events.
        # We only need to do this is the pause event was invoked by OctoEverywhere.
        #
        # Full details https://docs.octoprint.org/en/master/plugins/hooks.html#octoprint-comm-protocol-scripts
        # If we return something, we can return a prefix or a postfix, which will append the returned commands
        # before or after the event.
        if script_type != "gcode":
            return None

        if script_name  == "afterPrintPaused":
            # See if we need to handle print paused.
            # Swap anything that's pending with an empty list, so we only send the script once.
            localPauseScripts = self.PauseScripts
            self.PauseScripts = []
            if localPauseScripts is None or len(localPauseScripts) == 0:
                return None

            # Return the scripts to run AFTER the pause command executes.
            self.Logger.info("OctoPrint afterPrintPaused script fired and we are returning scripts: "+ self._ArrayToString(localPauseScripts))
            return None, localPauseScripts

        elif script_name == "beforePrintResumed":
            # See if we need to handle print resumed.
            # Swap anything that's pending with an empty list, so we only send the script once.
            localPrintResumed = self.ResumeScripts
            self.ResumeScripts = []
            if localPrintResumed is None or len(localPrintResumed) == 0:
                return None

            # Return the scripts to run BEFORE the pause command executes.
            self.Logger.info("OctoPrint beforePrintResumed script fired and we are returning scripts: "+ self._ArrayToString(localPrintResumed))
            return localPrintResumed, None

        # Default to not injecting any scripts.
        return None


    def OnGcodeQueuing(self, cmd):
        # We need to keep track of the current positioning modes so we can resume them
        # after we do the pause command.
        if cmd is None:
            return

        # Try to match the command. If we find it, use the exact command casing that was
        # originally sent.
        cmdLower = cmd.lower()
        if cmdLower == "g90" or cmdLower == "g91":
            self.LastG9Command = cmd
        if cmdLower == "m82" or cmdLower == "m83":
            self.LastM8Command = cmd


    def _ResetScripts(self):
        self.PauseScripts = []
        self.ResumeScripts = []
        self.LastPauseNotificationSuppressionTime = None


    def _ArrayToString(self, array):
        output = ""
        for element in array:
            output += element + ", "
        return output
