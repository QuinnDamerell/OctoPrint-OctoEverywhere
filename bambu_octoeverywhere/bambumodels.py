import time
import logging
from enum import Enum

from octoeverywhere.sentry import Sentry

# Known printer error types.
# Note that the print state doesn't have to be ERROR to have an error, during a print it's "PAUSED" but the print_error value is not 0.
# Here's the full list https://e.bambulab.com/query.php?lang=en
class BambuPrintErrors(Enum):
    Unknown = 1             # This will be most errors, since most of them aren't mapped
    FilamentRunOut = 2


# Since MQTT syncs a full state and then sends partial updates, we keep track of the full state
# and then apply updates on top of it. We basically keep a locally cached version of the state around.
class BambuState:

    def __init__(self) -> None:
        # We only parse out what we currently use.
        # We use the same naming as the json in the msg
        self.stg_cur:int = None
        self.gcode_state:str = None
        self.layer_num:int = None
        self.total_layer_num:int = None
        self.subtask_name:str = None
        self.mc_percent:int = None
        self.nozzle_temper:float = None
        self.nozzle_target_temper:float = None
        self.bed_temper:float = None
        self.bed_target_temper:float = None
        self.mc_remaining_time:int = None
        self.project_id:str = None
        self.print_error:int = None
        # On the X1, this is empty is LAN viewing of off
        # It's a URL if streaming is enabled
        # On other printers, this doesn't exist, so it's None
        self.rtsp_url:str = None
        # Custom fields
        self.LastTimeRemainingWallClock:float = None


    # Called when there's a new print message from the printer.
    def OnUpdate(self, msg:dict) -> None:
        # Get a new value or keep the current.
        # Remember that most of these are partial updates and will only have some values.
        self.stg_cur = msg.get("stg_cur", self.stg_cur)
        self.gcode_state = msg.get("gcode_state", self.gcode_state)
        self.layer_num = msg.get("layer_num", self.layer_num)
        self.total_layer_num = msg.get("total_layer_num", self.total_layer_num)
        self.subtask_name = msg.get("subtask_name", self.subtask_name)
        self.project_id = msg.get("project_id", self.project_id)
        self.mc_percent = msg.get("mc_percent", self.mc_percent)
        self.nozzle_temper = msg.get("nozzle_temper", self.nozzle_temper)
        self.nozzle_target_temper = msg.get("nozzle_target_temper", self.nozzle_target_temper)
        self.bed_temper = msg.get("bed_temper", self.bed_temper)
        self.bed_target_temper = msg.get("bed_target_temper", self.bed_target_temper)
        self.print_error = msg.get("print_error", self.print_error)
        ipCam = msg.get("ipcam", None)
        if ipCam is not None:
            self.rtsp_url = ipCam.get("rtsp_url", self.rtsp_url)

        # Time remaining has some custom logic, so as it's queried each time it keep counting down in seconds, since Bambu only gives us minutes.
        old_mc_remaining_time = self.mc_remaining_time
        self.mc_remaining_time = msg.get("mc_remaining_time", self.mc_remaining_time)
        if old_mc_remaining_time != self.mc_remaining_time:
            self.LastTimeRemainingWallClock = time.time()


    # Returns a time reaming value that counts down in seconds, not just minutes.
    # Returns null if the time is unknown.
    def GetContinuousTimeRemainingSec(self) -> int:
        if self.mc_remaining_time is None or self.LastTimeRemainingWallClock is None:
            return None
        # The slicer holds a constant time while in preparing, so we don't want to fake our countdown either.
        if self.IsPrepareOrSlicing():
            # Reset the last wall clock time to now, so when we transition to running, we don't snap to a strange offset.
            self.LastTimeRemainingWallClock = time.time()
            return int(self.mc_remaining_time * 60)
        # Compute the time based on when the value last updated.
        return int(max(0, (self.mc_remaining_time * 60) - (time.time() - self.LastTimeRemainingWallClock)))


    # Since there's a lot to consider to figure out if a print is running, this one function acts as common logic across the plugin.
    def IsPrinting(self, includePausedAsPrinting:bool) -> bool:
        return BambuState.IsPrintingState(self.gcode_state, includePausedAsPrinting)


    # We use this common method since "is this a printing state?" is complicated and we can to keep all of the logic common in the plugin
    @staticmethod
    def IsPrintingState(state:str, includePausedAsPrinting:bool) -> bool:
        if state is None:
            return False
        if state == "PAUSE" and includePausedAsPrinting:
            return True
        # Do we need to consider some of the stg_cur states?
        return state == "RUNNING" or BambuState.IsPrepareOrSlicingState(state)


    # We use this common method to keep all of the logic common in the plugin
    def IsPrepareOrSlicing(self) -> bool:
        return BambuState.IsPrepareOrSlicingState(self.gcode_state)


    # We use this common method to keep all of the logic common in the plugin
    @staticmethod
    def IsPrepareOrSlicingState(state:str) -> bool:
        if state is None:
            return False
        return state == "SLICING" or state == "PREPARE"


    # This one function acts as common logic across the plugin.
    def IsPaused(self) -> bool:
        if self.gcode_state is None:
            return False
        return self.gcode_state == "PAUSE"


    # If there is a file name, this returns it without the final .
    def GetFileNameWithNoExtension(self):
        if self.subtask_name is None:
            return None
        pos = self.subtask_name.rfind(".")
        if pos == -1:
            return self.subtask_name
        return self.subtask_name[:pos]


    # Returns a unique string for this print.
    # This string should be as unique as possible, but always the same for the same print.
    # If there is no active print, this should return None!
    # See details in NotificationHandler._RecoverOrRestForNewPrint
    def GetPrintCookie(self) -> str:
        # If there's no project id or subtask name, we shouldn't make a cookie..
        if self.project_id is None or len(self.project_id) == 0 or self.subtask_name is None or len(self.subtask_name) == 0:
            return None

        # From testing, the project_id is always unique for cloud based prints, but is 0 for local prints.
        # The file name changes most of the time, so the combination of both makes a good pair.
        return f"{self.project_id}-{self.GetFileNameWithNoExtension()}"


    # If the printer is in an error state, this tries to return the type, if known.
    # If the printer is not in an error state, None is returned.
    def GetPrinterError(self) -> BambuPrintErrors:
        # If there is a printer error, this is not 0
        if self.print_error is None or self.print_error == 0:
            return None

        # Oddly there are some errors that aren't errors? And the printer might sit in them while printing.
        # We ignore these. We also use the direct int values, so we don't have to build the hex string all of the time.
        # These error codes are in https://e.bambulab.com/query.php?lang=en, but have empty strings.
        # Hex: 05008030, 03008012, 0500C011
        if self.print_error == 83918896 or self.print_error == 50364434 or self.print_error == 83935249:
            return None

        # This state is when the user is loading filament, and the printer is asking them to push it in.
        # This isn't an error.
        if self.print_error == 134184967:
            return None

        # There's a full list of errors here, we only care about some of them
        # https://e.bambulab.com/query.php?lang=en
        # We format the error into a hex the same way the are on the page, to make it easier.
        # NOTE SOME ERRORS HAVE MULTIPLE VALUES, SO GET THEM ALL!
        # They have different values for the different AMS slots
        h = hex(self.print_error)[2:].rjust(8, '0')
        errorMap = {
            "07008011": BambuPrintErrors.FilamentRunOut,
            "07018011": BambuPrintErrors.FilamentRunOut,
            "07028011": BambuPrintErrors.FilamentRunOut,
            "07038011": BambuPrintErrors.FilamentRunOut,
            "07FF8011": BambuPrintErrors.FilamentRunOut,
        }
        return errorMap.get(h, BambuPrintErrors.Unknown)


# Different types of hardware.
class BambuPrinters(Enum):
    Unknown = 1
    X1C = 2
    X1E = 3
    P1P = 10
    P1S = 11
    A1  = 20
    A1Mini = 21


class BambuCPUs(Enum):
    Unknown = 1
    ESP32 = 2  # Lower powered CPU used on the A1 and P1P
    RV1126= 3  # High powered CPU used on the X1 line


# Tracks the version info.
class BambuVersion:

    def __init__(self, logger:logging.Logger) -> None:
        self.Logger = logger
        self.HasLoggedPrinterVersion = False
        # We only parse out what we currently use.
        self.SoftwareVersion:str = None
        self.HardwareVersion:str = None
        self.SerialNumber:str = None
        self.ProjectName:str = None
        self.Cpu:BambuCPUs = None
        self.PrinterName:BambuPrinters = None


    # Called when there's a new print message from the printer.
    def OnUpdate(self, msg:dict) -> None:
        module = msg.get("module", None)
        if module is None:
            return
        for m in module:
            name = m.get("name", None)
            if name is None:
                continue
            if name == "ota":
                self.SoftwareVersion = m.get("sw_ver", self.SoftwareVersion)
            elif name == "mc":
                self.SerialNumber = m.get("sn", self.SerialNumber)
            elif name == "esp32":
                self.HardwareVersion = m.get("hw_ver", self.HardwareVersion)
                self.ProjectName = m.get("project_name", self.ProjectName)
                self.Cpu = BambuCPUs.ESP32
            elif name == "rv1126":
                self.HardwareVersion = m.get("hw_ver", self.HardwareVersion)
                self.ProjectName = m.get("project_name", self.ProjectName)
                self.Cpu = BambuCPUs.RV1126

        # If we didn't find a hardware, it's unknown.
        if self.Cpu is None:
            self.Cpu = BambuCPUs.Unknown

        # Now that we have info, map the printer type.
        if self.Cpu is not BambuCPUs.Unknown and self.HardwareVersion is not None:
            if self.Cpu is BambuCPUs.RV1126:
                # Map for RV1126 CPU
                rv1126_map = {
                    "AP05": BambuPrinters.X1C,
                    "AP02": BambuPrinters.X1E,
                    # Add more mappings here as needed
                }
                self.PrinterName = rv1126_map.get(self.HardwareVersion, BambuPrinters.Unknown)

            elif self.Cpu is BambuCPUs.ESP32 and self.ProjectName is not None:
                # Map for ESP32 CPU
                esp32_map = {
                    ("AP04", "C11"): BambuPrinters.P1P,
                    ("AP04", "C12"): BambuPrinters.P1S,
                    ("AP05", "N1"): BambuPrinters.A1Mini,
                    ("AP05", "N2S"): BambuPrinters.A1,
                    ("AP07", "N1"): BambuPrinters.A1Mini,
                    # Add more mappings here as needed
                }
                self.PrinterName = esp32_map.get((self.HardwareVersion, self.ProjectName), BambuPrinters.Unknown)

        if self.PrinterName is None or self.PrinterName is BambuPrinters.Unknown:
            Sentry.LogError(f"Unknown printer type. CPU:{self.Cpu}, Project Name: {self.ProjectName}, Hardware Version: {self.HardwareVersion}",{
                "CPU": str(self.Cpu),
                "ProjectName": str(self.ProjectName),
                "HardwareVersion": str(self.HardwareVersion),
                "SoftwareVersion": str(self.SoftwareVersion),
            })
            self.PrinterName = BambuPrinters.Unknown

        if self.HasLoggedPrinterVersion is False:
            self.HasLoggedPrinterVersion = True
            self.Logger.info(f"Printer Version: {self.PrinterName}, CPU: {self.Cpu}, Project: {self.ProjectName} Hardware: {self.HardwareVersion}, Software: {self.SoftwareVersion}, Serial: {self.SerialNumber}")
