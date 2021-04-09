# Respresents the header class we encode in json.
class Utils:

    @staticmethod
    def IsWebcamRequest(path) :
        return path.lower().find("/webcam/") != -1

    @staticmethod
    def GetWebcamRequestPath(path, localAddress, mjpgStreamerPort) :
        # When we are talking to mjpg-streamer, we will talk directly to
        # it's http server. For that reason, we need to remove the /webcam/
        # which usually maps the requests to mjpg-streamer for the http-proxy
        webcamPathStart = path.lower().find("/webcam/")
        if webcamPathStart == -1:
            return ""

        # Skip the webcam path
        webcamPathStart += len("/webcam/")

        # Return the full path
        return 'http://' + str(localAddress) + ':' + str(mjpgStreamerPort) + '/' + path[webcamPathStart:]

    # Given an OctoMessage and the correct params, this function returns the absolute URI that should be
    # requested. This is dynamic based on the message type.
    @staticmethod
    def GetOctoMessageAbsoluteUri(msg, localHostAddress, localHostPort, mjpgStreamerLocalPort) :
        
        if "Path" in msg and msg["Path"] != None and len(msg["Path"]) > 0:
            
            # If we have the Path var, it means the http request is relative to this device.
            path = msg["Path"]

            # We make all relative local requests to either OctoPrint or mjpg-streamer directly, instead of jumping
            # through the http proxy running on port 80. We do this for two reasons, 1) perf 2) some users don't have
            # the http proxy running on port 80. 
            #
            # However, this does break user scenarios where they have a different httpproxy config than default.
            # For example, if they bing another mjpg-streamer instance to some random port and give it a random http path
            # we have no way of detecting that and we will send the request to OctoPrint. Common multicam setups run
            # on /webcam2/, /webcam3/ etc.
            # TODO ideally we should parse the httpproxy config to find any special user configs.
            #
            # Any path that is directed to /webcam/ needs to go to mjpg-streamer instead of
            # the OctoPrint instance. If we detect it, we need to use a different path.
            if Utils.IsWebcamRequest(path) :
                # Make the special webcam stream path.
                return Utils.GetWebcamRequestPath(path, localHostAddress, mjpgStreamerLocalPort)
            else :
                # If this isn't a webcam stream, connect to the OctoPrint instance.
                return "http://" + localHostAddress + ":" + str(localHostPort) + path

        elif "AbsUrl" in msg:
            # If we get an absolute URL we need to make this request directly to it. This could be a 
            # different port on this device or another device's IP on the same LAN.
            # This is used for some users who setup multiple cameras on different port, or even 
            # cameras that run on other devices on the network.
            return msg["AbsUrl"]
        else:
            return "unknown"