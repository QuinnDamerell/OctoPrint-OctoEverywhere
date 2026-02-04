import logging
import threading
from typing import Any, List, Optional, Dict, Tuple

from .moonrakerclient import MoonrakerClient


class LightStatus:
    def __init__(self, name:str, isOn:bool):
        self.Name = name
        self.IsOn = isOn


# This class manages light detection and control for Moonraker/Klipper.
# It detects common light configurations and provides methods to query status and control them.
class LightManager:

    # Common names used for case/chamber lights in Klipper configs
    COMMON_LIGHT_NAMES = [
        "caselight",
        "case_light",
        "light",
        "lights",
        "chamber_light",
        "chamberlight",
        "led_light",
        "ledlight",
        "cavity_led", # Used by the Snapmaker U1
    ]


    # Logic for a static singleton
    _Instance: "LightManager" = None #pyright: ignore[reportAssignmentType]


    @staticmethod
    def Init(logger: logging.Logger):
        LightManager._Instance = LightManager(logger)


    @staticmethod
    def Get() -> "LightManager":
        return LightManager._Instance


    def __init__(self, logger: logging.Logger) -> None:
        self.Logger = logger
        # Cache of detected Klipper lights: key=light_name, value=(type, full_object_name)
        # type can be: "output_pin", "led", "neopixel", "dotstar"
        self.DetectedLights: Dict[str, Tuple[str, str]] = {}
        self.DetectedLightsLock = threading.Lock()
        # Cache of detected Moonraker power devices: key=device_name, value=device_name
        self.DetectedPowerDevices: Dict[str, str] = {}
        self.DetectedPowerDevicesLock = threading.Lock()
        # Combined flag for any lights detected
        self.LightsDetected = False
        self.DetectionAttempted = False


    # Detects available lights in the printer configuration.
    # This should be called after the Moonraker connection is established.
    def DetectLights(self, force:bool=False) -> None:
        if self.DetectionAttempted and not force:
            return

        self.DetectionAttempted = True
        self.Logger.debug("LightManager: Detecting available lights...")

        try:
            # Query all available printer objects
            result = MoonrakerClient.Get().SendJsonRpcRequest("printer.objects.list")
            if result.HasError():
                self.Logger.warning("LightManager: Failed to query printer objects for light detection: " + result.GetLoggingErrorStr())
                return

            # Get the list of objects
            objects = result.GetResult().get("objects", [])
            if not objects:
                self.Logger.debug("LightManager: No printer objects found")
                return

            # Look for lights in the objects list
            detected_lights: Dict[str, Tuple[str, str]] = {}
            for obj in objects:
                obj_lower = obj.lower()

                # Check for output_pin objects with common light names
                if obj_lower.startswith("output_pin "):
                    pin_name = obj[11:].strip()  # Remove "output_pin " prefix
                    pin_name_lower = pin_name.lower()
                    for light_name in self.COMMON_LIGHT_NAMES:
                        if light_name in pin_name_lower:
                            detected_lights[pin_name] = ("output_pin", obj)
                            self.Logger.info(f"LightManager: Detected output_pin light: {pin_name}")
                            break

                # Check for LED-based lights
                elif obj_lower.startswith("led "):
                    led_name = obj[4:].strip()
                    led_name_lower = led_name.lower()
                    for light_name in self.COMMON_LIGHT_NAMES:
                        if light_name in led_name_lower:
                            detected_lights[led_name] = ("led", obj)
                            self.Logger.info(f"LightManager: Detected LED light: {led_name}")
                            break

                # Check for neopixel lights
                elif obj_lower.startswith("neopixel "):
                    neopixel_name = obj[9:].strip()
                    neopixel_name_lower = neopixel_name.lower()
                    for light_name in self.COMMON_LIGHT_NAMES:
                        if light_name in neopixel_name_lower:
                            detected_lights[neopixel_name] = ("neopixel", obj)
                            self.Logger.info(f"LightManager: Detected neopixel light: {neopixel_name}")
                            break

                # Check for dotstar lights
                elif obj_lower.startswith("dotstar "):
                    dotstar_name = obj[8:].strip()
                    dotstar_name_lower = dotstar_name.lower()
                    for light_name in self.COMMON_LIGHT_NAMES:
                        if light_name in dotstar_name_lower:
                            detected_lights[dotstar_name] = ("dotstar", obj)
                            self.Logger.info(f"LightManager: Detected dotstar light: {dotstar_name}")
                            break

            # Also check for Moonraker power devices (smart plugs, relays, etc.)
            detected_power_devices = self._DetectPowerDevices()

            # Update the cached values
            with self.DetectedLightsLock:
                self.DetectedLights = detected_lights
            with self.DetectedPowerDevicesLock:
                self.DetectedPowerDevices = detected_power_devices

            # Update the combined flag
            total_lights = len(detected_lights) + len(detected_power_devices)
            if total_lights > 0:
                self.LightsDetected = True
                self.Logger.debug("LightManager: Detection complete. Found %s Klipper light(s) and %s power device(s)",
                                 len(detected_lights), len(detected_power_devices))
            else:
                self.Logger.debug("LightManager: No lights detected in printer configuration")

        except Exception as e:
            self.Logger.error(f"LightManager: Exception during light detection: {e}")


    # Helper method to detect Moonraker power devices (smart plugs, relays, etc.)
    # Returns a dict of detected power devices: key=device_name, value=device_name
    def _DetectPowerDevices(self) -> Dict[str, str]:
        detected_power_devices: Dict[str, str] = {}
        try:
            # Query Moonraker for power devices
            result = MoonrakerClient.Get().SendJsonRpcRequest("machine.device_power.devices")

            if result.HasError():
                self.Logger.debug("LightManager: No power devices endpoint available or failed to query")
                return detected_power_devices

            # Get the devices list
            devices = result.GetResult().get("devices", [])
            if not devices:
                self.Logger.debug("LightManager: No power devices found")
                return detected_power_devices

            # Look for devices with light-related names
            for device in devices:
                if isinstance(device, dict):
                    device:Dict[str, Any] = device
                    device_name = device.get("device", "")
                    device_name_lower = device_name.lower()

                    # Check if this device name matches common light names
                    for light_name in self.COMMON_LIGHT_NAMES:
                        if light_name in device_name_lower:
                            # Store the device name
                            detected_power_devices[device_name] = device_name
                            self.Logger.info(f"LightManager: Detected power device light: {device_name}")
                            break

        except Exception as e:
            self.Logger.debug(f"LightManager: Exception detecting power devices (this is normal if not configured): {e}")

        return detected_power_devices


    # Returns True if any lights were detected
    def HasLights(self) -> bool:
        if not self.DetectionAttempted:
            self.DetectLights()
        return self.LightsDetected


    # Returns a dictionary of light object names to query.
    # This should be merged into the printer.objects.query request.
    # Returns an empty dict if no lights are detected.
    def GetLightObjectNames(self) -> Dict[str, None]:
        if not self.HasLights():
            return {}

        # Return all detected light object names for querying
        query_objects = {}
        with self.DetectedLightsLock:
            for _, (_, full_object_name) in self.DetectedLights.items():
                query_objects[full_object_name] = None

        return query_objects


    # Gets the status of all detected lights from a query result.
    # status_dict: The "status" dictionary from a printer.objects.query result
    # Returns a list of LightStatus objects, or None if no lights or error
    def GetLightStatus(self, status_dict: Dict[str, Any]) -> Optional[List[LightStatus]]:

        # Return early if no lights detected
        if not self.HasLights():
            return None

        try:
            light_statuses: List[LightStatus] = []

            # Process Klipper lights from the query result
            with self.DetectedLightsLock:
                for light_name, (light_type, full_object_name) in self.DetectedLights.items():
                    # Get the light status from the query result
                    light_status = status_dict.get(full_object_name, {})
                    if not light_status:
                        self.Logger.debug(f"LightManager: No status data for light '{light_name}'")
                        continue

                    # Determine if the light is on based on its type
                    is_on = False
                    if light_type == "output_pin":
                        # For output_pin, check the "value" field
                        value = light_status.get("value", 0.0)
                        is_on = float(value) > 0.0

                    elif light_type in ["led", "neopixel", "dotstar"]:
                        # For LED types, check the color_data field
                        # If any LED has non-zero values, consider it "on"
                        color_data = light_status.get("color_data", [])
                        if isinstance(color_data, list) and len(color_data) > 0:
                            # color_data is a list of [R, G, B, W] tuples
                            for color_tuple in color_data:
                                if any(c > 0.0 for c in color_tuple):
                                    is_on = True
                                    break

                    # Add to the results
                    light_statuses.append(LightStatus(light_name, is_on))

            # Process power devices separately, if we have any.
            hasPowerDeviceLights = False
            with self.DetectedPowerDevicesLock:
                if len(self.DetectedPowerDevices) > 0:
                    hasPowerDeviceLights = True
            if hasPowerDeviceLights:
                power_device_status = self._GetPowerDeviceStatus()
                if power_device_status:
                    with self.DetectedPowerDevicesLock:
                        for device_name, _ in self.DetectedPowerDevices:
                            if device_name in power_device_status:
                                is_on = power_device_status[device_name]
                                light_statuses.append(LightStatus(device_name, is_on))
                            else:
                                self.Logger.debug(f"LightManager: No power device status for '{device_name}'")

            return light_statuses if len(light_statuses) > 0 else None

        except Exception as e:
            self.Logger.error(f"LightManager: Exception getting light status: {e}")

        return None


    # Helper method to get the status of all power devices
    # Returns a dict mapping device_name -> is_on (bool), or None on error
    def _GetPowerDeviceStatus(self) -> Optional[Dict[str, bool]]:
        try:
            result = MoonrakerClient.Get().SendJsonRpcRequest("machine.device_power.devices")
            if result.HasError():
                return None

            devices = result.GetResult().get("devices", [])
            status_dict = {}

            for device in devices:
                if isinstance(device, dict):
                    device:Dict[str, Any] = device
                    device_name = device.get("device", "")
                    # Status can be "on", "off", or other values
                    status = device.get("status", "off")
                    status_dict[device_name] = status == "on"

            return status_dict

        except Exception as e:
            self.Logger.debug(f"LightManager: Exception getting power device status: {e}")
            return None


    # Sets the light state (on or off) for a specific light by name.
    # If name is empty, controls the first detected light.
    # Returns True if successful, False otherwise
    def SetLightState(self, name: str, on: bool) -> bool:
        if not self.HasLights():
            self.Logger.warning("LightManager: Cannot set light state, no lights detected")
            return False

        try:
            # Find the light to control - check Klipper lights first
            target_light_name = None
            light_type = None
            is_power_device = False

            with self.DetectedLightsLock:
                if name and name in self.DetectedLights:
                    # Found in Klipper lights
                    target_light_name = name
                    light_type, _ = self.DetectedLights[name]

            # If not found in Klipper lights, check power devices
            if target_light_name is None:
                with self.DetectedPowerDevicesLock:
                    if name and name in self.DetectedPowerDevices:
                        # Found in power devices
                        target_light_name = name
                        is_power_device = True

            if target_light_name is None:
                self.Logger.error(f"LightManager: Set light was called with an unknown light name {name}")
                return False

            self.Logger.info(f"LightManager: Setting light '{target_light_name}' to {'ON' if on else 'OFF'}")

            # Handle power devices separately using Moonraker API
            if is_power_device:
                return self._SetPowerDeviceState(target_light_name, on)

            # For Klipper objects, use G-code commands
            gcode = None
            if light_type == "output_pin":
                # For output_pin, use SET_PIN command
                value = 1.0 if on else 0.0
                gcode = f"SET_PIN PIN={target_light_name} VALUE={value}"

            elif light_type in ["led", "neopixel", "dotstar"]:
                # For LED types, use SET_LED command
                if on:
                    # Set to white at full brightness
                    gcode = f"SET_LED LED={target_light_name} RED=1 GREEN=1 BLUE=1 WHITE=1"
                else:
                    # Turn off
                    gcode = f"SET_LED LED={target_light_name} RED=0 GREEN=0 BLUE=0 WHITE=0"
            else:
                self.Logger.error(f"LightManager: Unknown light type: {light_type}")
                return False

            # Execute the gcode command
            result = MoonrakerClient.Get().SendJsonRpcRequest("printer.gcode.script", {
                "script": gcode
            })

            if result.HasError():
                self.Logger.error(f"LightManager: Failed to set light state: {result.GetLoggingErrorStr()}")
                return False

            self.Logger.info(f"LightManager: Successfully set light to {'ON' if on else 'OFF'}")
            return True

        except Exception as e:
            self.Logger.error(f"LightManager: Exception setting light state: {e}")
            return False


    # Helper method to control a Moonraker power device
    def _SetPowerDeviceState(self, device_name: str, on: bool) -> bool:
        try:
            action = "on" if on else "off"
            result = MoonrakerClient.Get().SendJsonRpcRequest("machine.device_power.device", {
                "device": device_name,
                "action": action
            })

            if result.HasError():
                self.Logger.error(f"LightManager: Failed to set power device state: {result.GetLoggingErrorStr()}")
                return False

            self.Logger.info(f"LightManager: Successfully set power device to {action.upper()}")
            return True

        except Exception as e:
            self.Logger.error(f"LightManager: Exception setting power device state: {e}")
            return False
