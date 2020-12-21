# Respresents the header class we encode in json.
class Utils:

    @staticmethod
    def IsWebcamRequest(path) :
        return path.lower().find("/webcam/") != -1

    @staticmethod
    def GetWebcamRequestPath(path, localAddress, mjpgStreamerPort) :
        # When we are talking to mjpg-streamer, we will talk directly to
        # it's http server. For that reason, we need to remove the /webcam/
        # which usually maps the requrst to mjpg-streamer for the http-proxy
        webcamPathStart = path.lower().find("/webcam/")
        if webcamPathStart == -1:
            return ""

        # Skip the webcam path
        webcamPathStart += len("/webcam/")

        # Return the full path
        return 'http://' + str(localAddress) + ':' + str(mjpgStreamerPort) + '/' + path[webcamPathStart:]