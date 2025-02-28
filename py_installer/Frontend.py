from enum import Enum
import requests

from linux_host.networksearch import NetworkSearch

from .Util import Util
from .Logging import Logger
from .Context import Context
from .ConfigHelper import ConfigHelper

# Frontends that are known.
class KnownFrontends(Enum):
    Unknown  = 1
    Mainsail = 2
    Fluidd   = 3
    Creality = 4 # This is Creality's K1 default web interface (not nearly as good as the others)
    Elegoo   = 5 # This is Elegoo's default web interface.

    # Makes to str() cast not to include the class name.
    def __str__(self):
        return self.name

# A helper class.
class DiscoveryPair:
    def __init__(self, port:int, frontend:KnownFrontends) -> None:
        self.Port:int = port
        self.Frontend = frontend


# A Class to help with detecting and setting up a frontend.
class Frontend:

    # If called, this will walk the user though picking a frontend for the targeted device (local or remote for companion).
    # The frontend will be saved into the OE config, so this should be done before the service starts, if this is a first time run.
    def DoFrontendSetup(self, context:Context):

        # There's no frontend for bambu connect.
        if context.IsBambuSetup:
            Logger.Debug("Skipping frontend setup, there's no frontend for bambu connect.")
            return

        # The elegoo os only allows for one frontend and doesn't have printer access, so we don't need to ask the user.
        if context.IsElegooSetup:
            Logger.Debug("Skipping frontend setup, elegoo os only allows for one frontend.")
            ConfigHelper.WriteFrontendDetails(context, str(NetworkSearch.c_ElegooDefaultPortStr), KnownFrontends.Elegoo)
            return

        Logger.Debug("Starting Web Interface Setup")

        # Try to get the existing configured port.
        (currentPort, frontendHint_CanBeNone) = ConfigHelper.TryToGetFrontendDetails(context)
        if currentPort is not None:
            # There is already a config with a port setup.
            # Ask if the user wants to keep the current setup.
            Logger.Blank()
            Logger.Blank()
            Logger.Info("A web interface is already setup:")
            msg = ""
            if frontendHint_CanBeNone is not None and frontendHint_CanBeNone.lower() != str(KnownFrontends.Unknown):
                msg += frontendHint_CanBeNone
            else:
                msg += "An unknown web interface"
            msg += f" on port {currentPort}"
            Logger.Header(msg)
            Logger.Blank()
            if Util.AskYesOrNoQuestion("Do you want to keep this setup?"):
                # Keep the current setup
                return

        # Get a frontend port from the user.
        (portInt, frontendHint_CanBeNone) = self._GetDesiredFrontend(context)

        # We got a port, save it.
        ConfigHelper.WriteFrontendDetails(context, str(portInt), frontendHint_CanBeNone)


    # Returns the (port (int), frontendNameHint:str or None) of the frontend the user wants to use.
    def _GetDesiredFrontend(self, context:Context):
        # Find the target. If this is a local install, the target is local.
        # Otherwise, it's whatever the companion target is.
        targetIpOrHostname = "127.0.0.1"
        if context.IsCompanionBambuOrElegoo():
            (ip, _) = ConfigHelper.TryToGetCompanionDetails(context)
            if ip is None or len(ip) == 0:
                raise Exception("Frontend setup failed to find companion ip from companion config file.")
            targetIpOrHostname = ip

        # Try to discover any known frontends
        foundFrontends = self._DiscoverKnownFrontends(targetIpOrHostname)

        # If we found something, ask the user if they want to use one.
        if len(foundFrontends) > 0:
            # A lot of users seem to be confused by this frontend setup, so if there's only one interface, we will just use it.
            if len(foundFrontends) == 1:
                item = foundFrontends[0]
                Logger.Info(f"Only one frontend was found [{str(item.Frontend)} - {str(item.Port)}] so we will use it for remote access.")
                return (item.Port, str(item.Frontend))

            Logger.Blank()
            Logger.Info("The following web interfaces were discovered:")
            count = 0
            # List them in the order we found them, since we order the port list in by priority.
            for f in foundFrontends:
                count += 1
                Logger.Info(f"  {count}) {str(f.Frontend).ljust(8)} - Port {str(f.Port)}")
            Logger.Blank()
            while True:
                response = input("From the list above, enter the number of the web interface you would like to use for remote access; or enter `m` to manually setup the web interface: ")
                response = response.lower().strip()
                if response == "m":
                    # Break to fall through to the manual setup.
                    break
                try:
                    # Parse the input and -1 it, so it aligns with the array length.
                    tempInt = int(response.lower().strip()) - 1
                    if tempInt >= 0 and tempInt < len(foundFrontends):
                        item = foundFrontends[tempInt]
                        return (item.Port, str(item.Frontend))
                except Exception as _:
                    Logger.Warn("Invalid input, try again.")
        else:
            Logger.Debug("No web interfaces could be detected.")


        # If we are here, the user selected m to do a manual frontend setup.
        firstTry = True
        while True:
            try:
                Logger.Blank()
                Logger.Blank()
                Logger.Blank()
                Logger.Info("Enter the port number or web address you use to access your printer.")
                Logger.Info("   Examples of port could be something like:")
                Logger.Info("        80")
                Logger.Info("        4408")
                Logger.Info("   Examples web address could be something like:")
                Logger.Info("        192.168.1.15:4409")
                Logger.Info("        https://192.168.1.15:4409/")
                Logger.Info("        myprinter.local:4409")
                Logger.Blank()
                if firstTry is False:
                    Logger.Info("If you're having trouble, contact us at support@octoeverywhere.com")
                    Logger.Blank()
                firstTry = False
                response = input("Please enter the port or web address: ")
                response = response.lower().strip()
                ogResponse = response


                # Parse the response. Start assuming a full web address, and strip down.
                #  Inputs could be:
                #     https://127.0.0.1/
                #     klipper.local:80
                #     127.0.0.1
                #     ...

                # First remote any protocol
                searchStr = "://"
                if response.find(searchStr) != -1:
                    response = response[response.find(searchStr) + len(searchStr):]
                # Next, remove anything before the next :, which would be the domain or IP
                if response.find(":") != -1:
                    response = response[response.find(":")+1:]
                else:
                    # If there's no : it means
                    #   This string is only a port
                    #   The string is URL with no port.
                    # If it's a URL, it must have a . for the domain
                    if response.find(".") != -1 or response.find("localhost") != -1:
                        # This is a url wit no port.
                        if ogResponse.find("https") != -1:
                            response = "443"
                        else:
                            response = "80"
                    else:
                        # The input is just a port.
                        pass
                # Finally, remove any trailing path stuff
                if response.find("/") != -1:
                    response = response[:response.find("/")]

                # Ensure it's an int.
                try:
                    int(response)
                except Exception as e:
                    Logger.Debug(f"Error: {str(e)}")
                    Logger.Warn(f"Input of `{response}` isn't a valid port, it must be a number. Please try again.")
                    continue

                # Test to see if we can find a frontend.
                Logger.Debug(f"Final port response `{response}`")
                (isValid, _, frontend) = self.CheckIfValidFrontend(targetIpOrHostname, response)

                # Report the result.
                Logger.Blank()
                if isValid:
                    if frontend != KnownFrontends.Unknown:
                        Logger.Info(f"We detected {str(frontend)} running on the port or web address you entered.")
                    else:
                        Logger.Info("We detected a web interface running on the port or web address you entered.")
                else:
                    Logger.Warn(f"We DIDN'T detect a web interface running on port {response}, which you entered.")

                # Ask the user, let them return it even if it's invalid.
                if Util.AskYesOrNoQuestion("Is this the web interface you want to use?"):
                    if frontend != KnownFrontends.Unknown:
                        return (int(response), str(frontend))
                    else:
                        return (int(response), None)

            except Exception as e:
                Logger.Debug(f"Manual frontend error {str(e)}")
                Logger.Warn("Error, please try again.")


    # If we can find a known frontend, this will return it.
    # Returns a list of DiscoveryPair, if any are found.
    def _DiscoverKnownFrontends(self, ipOrHostname:str):
        # We can't scan all ports, it will take to long. Instead we will scan known ports used by known common setups.
        # Note these ports are in order of importance, where ports near the top are more likely to be what the user wants.
        knownFrontendPorts = [
            4408, # On the K1, this is the Fluidd port. (This is set by the install script from GitHub Guilouz/Creality-K1-and-K1-Max)
            4409, # On the K1, this is the mainsail port. (This is set by the install script from GitHub Guilouz/Creality-K1-and-K1-Max)
            80,   # On most devices, this is the port the frontend is on. But note on the K1, this is Creality's own special frontend, most users don't want.
            81,   # A common port for an secondary frontend to run on, like Fluidd or Mainsail.
            443,  # Not ideal, but https might be here.
            8819, # Sonic Pad Mainsail port.
            3030, # This is the web interface port for the Elegoo OS printers.
        ]

        # Try to find what we can.
        foundFrontends = []
        for port in knownFrontendPorts:
            (isValid, _, frontend) = self.CheckIfValidFrontend(ipOrHostname, str(port))
            if isValid:
                foundFrontends.append(DiscoveryPair(port, frontend))

        # Return anything we got.
        return foundFrontends


    # Checks if the hostname and ip are a valid http endpoint.
    # Returns (isValid:bool, isHttps:bool, frontendName:KnownFrontends)
    def CheckIfValidFrontend(self, ipOrHostname:str, portStr:str, timeoutSec:float = 2.0):
        try:
            # Don't allow redirects, so we can detect https upgrades redirects.
            url = f"http://{ipOrHostname}:{portStr}"
            result = requests.get(url=url, timeout=timeoutSec, allow_redirects=False)

            # Handle error codes.
            if result.status_code == 301 or  result.status_code == 308 or result.status_code == 307:
                location = None
                if "location" in result.headers:
                    location = result.headers["location"]
                    Logger.Warn(f"{url} resulted in a redirect request to {location}")
                raise Exception(f"Redirect to {location} requested.")
            if result.status_code != 200:
                raise Exception(f"Status code was not 200. Code: {result.status_code}")

            # We have a valid http response, try to figure out the frontend if possible.
            # If we fail, we still want to return valid.
            frontend = KnownFrontends.Unknown
            try:
                htmlLower = result.text.lower()
                if "mainsail" in htmlLower:
                    frontend = KnownFrontends.Mainsail
                elif "fluidd" in htmlLower:
                    frontend = KnownFrontends.Fluidd
                elif "creality" in htmlLower:
                    frontend = KnownFrontends.Creality
                elif "elegoo" in htmlLower:
                    frontend = KnownFrontends.Elegoo
                else:
                    Logger.Debug(f"Unknown frontend type. html: {result.text}")
            except Exception as e:
                Logger.Debug(f"Failed to figure out http frontend. {e}")

            # We got a valid http response, so we pass
            return (True, False, frontend)

        except Exception as e:
            Logger.Debug(f"Frontend check failed. {ipOrHostname}:{portStr} {str(e)}")
        # Return failed.
        return (False, False, KnownFrontends.Unknown)
