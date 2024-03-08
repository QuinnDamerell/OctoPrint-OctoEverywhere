import time
import logging
from enum import Enum


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
        self.gcode_file:str = None
        self.mc_percent:int = None
        self.nozzle_temper:int = None
        self.nozzle_target_temper:int = None
        self.bed_temper:int = None
        self.bed_target_temper:int = None
        self.mc_remaining_time:int = None
        self.project_id:str = None
        self.print_error:int = None
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
        self.gcode_file = msg.get("gcode_file", self.gcode_file)
        self.project_id = msg.get("project_id", self.project_id)
        self.mc_percent = msg.get("mc_percent", self.mc_percent)
        self.nozzle_temper = msg.get("nozzle_temper", self.nozzle_temper)
        self.nozzle_target_temper = msg.get("nozzle_target_temper", self.nozzle_target_temper)
        self.bed_temper = msg.get("bed_temper", self.bed_temper)
        self.bed_target_temper = msg.get("bed_target_temper", self.bed_target_temper)
        self.print_error = msg.get("print_error", self.print_error)

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
        if self.gcode_state == "SLICING" or self.gcode_state == "PREPARE":
            # Reset the last wall clock time to now, so when we transition to running, we don't snap to a strange offset.
            self.LastTimeRemainingWallClock = time.time()
            return self.mc_remaining_time * 60.0
        # Compute the time based on when the value last updated.
        return int(max(0, (self.mc_remaining_time * 60) - (time.time() - self.LastTimeRemainingWallClock)))


    # Since there's a lot to consider to figure out if a print is running, this one function acts as common logic across the plugin.
    def IsPrinting(self, includePausedAsPrinting:bool) -> bool:
        if self.gcode_state is None:
            return False
        if self.gcode_state == "PAUSE" and includePausedAsPrinting:
            return True
        # Do we need to consider some of the stg_cur states?
        return self.gcode_state == "RUNNING" or self.gcode_state == "SLICING" or self.gcode_state == "PREPARE"


    # This one function acts as common logic across the plugin.
    def IsPaused(self) -> bool:
        if self.gcode_state is None:
            return False
        return self.gcode_state == "PAUSE"


    # If there is a file name, this returns it without the final .
    def GetFileNameWithNoExtension(self):
        if self.gcode_file is None:
            return None
        pos = self.gcode_file.rfind(".")
        if pos == -1:
            return self.gcode_file
        return self.gcode_file[:pos]


    # Returns a unique string for this print.
    # This string should be as unique as possible, but always the same for the same print.
    # See details in NotificationHandler._RecoverOrRestForNewPrint
    def GetPrintCookie(self) -> str:
        # From testing, the project_id is always unique for cloud based prints, but is 0 for local prints.
        # The file name changes most of the time, so the combination of both makes a good pair.
        return f"{self.project_id}-{self.GetFileNameWithNoExtension()}"


    # If the printer is in an error state, this tries to return the type, if known.
    # If the printer is not in an error state, None is returned.
    def GetPrinterError(self) -> BambuPrintErrors:
        # If there is a printer error, this is not 0
        if self.print_error is None or self.print_error == 0:
            return None
        # There's a full list of errors here, we only care about some of them
        # https://e.bambulab.com/query.php?lang=en
        # We format the error into a hex the same way the are on the page, to make it easier.
        # NOTE SOME ERRORS HAVE MULTPLE VALUES, SO GET THEM ALL!
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
        if self.Cpu is not BambuCPUs.Unknown and self.HardwareVersion is not None and self.ProjectName is not None:
            if self.Cpu is BambuCPUs.RV1126:
                if self.HardwareVersion == "AP05":
                    self.PrinterName = BambuPrinters.X1C
                elif self.HardwareVersion == "AP02":
                    self.PrinterName = BambuPrinters.X1E
            if self.Cpu is BambuCPUs.ESP32:
                if self.HardwareVersion == "AP04":
                    if self.ProjectName == "C11":
                        self.PrinterName = BambuPrinters.P1P
                    if self.ProjectName == "C12":
                        self.PrinterName = BambuPrinters.P1S
                if self.HardwareVersion == "AP05":
                    if self.ProjectName == "N1":
                        self.PrinterName = BambuPrinters.A1Mini
                    if self.ProjectName == "N2S":
                        self.PrinterName = BambuPrinters.A1

        if self.PrinterName is None or self.PrinterName is BambuPrinters.Unknown:
            self.Logger.warn(f"Unknown printer type. CPU:{self.Cpu}, Project Name: {self.ProjectName}, Hardware Version: {self.HardwareVersion}")
            self.PrinterName = BambuPrinters.Unknown

        if self.HasLoggedPrinterVersion is False:
            self.HasLoggedPrinterVersion = True
            self.Logger.info(f"Printer Version: {self.PrinterName}, CPU: {self.Cpu}, Project: {self.ProjectName} Hardware: {self.HardwareVersion}, Software: {self.SoftwareVersion}, Serial: {self.SerialNumber}")
