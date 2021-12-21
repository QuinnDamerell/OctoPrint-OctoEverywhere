import requests
from octoprint_octoeverywhere.Proto.PathTypes import PathTypes
from .localip import LocalIpHelper
from .octostreammsgbuilder import OctoStreamMsgBuilder

class OctoHttpRequest:
    LocalHttpProxyPort = 80
    LocalHttpProxyIsHttps = False
    LocalOctoPrintPort = 5000
    LocalHostAddress = "127.0.0.1"

    @staticmethod
    def SetLocalHttpProxyPort(port):
        OctoHttpRequest.LocalHttpProxyPort = port

    @staticmethod
    def SetLocalHttpProxyIsHttps(isHttps):
        OctoHttpRequest.LocalHttpProxyIsHttps = isHttps

    @staticmethod
    def GetLocalHttpProxyPort():
        return OctoHttpRequest.LocalHttpProxyPort

    @staticmethod
    def SetLocalOctoPrintPort(port):
        OctoHttpRequest.LocalOctoPrintPort = port

    @staticmethod
    def GetLocalOctoPrintPort():
        return OctoHttpRequest.LocalOctoPrintPort

    @staticmethod
    def GetLocalhostAddress():
        return OctoHttpRequest.LocalHostAddress

    @staticmethod
    def SetLocalhostAddress(address):
        OctoHttpRequest.LocalHostAddress = address

    # The result of a successfully made http request.
    # "successfully made" means we talked to the server, not the the http
    # response is good.
    class Result():
        def __init__(self, result, url, didFallback):
            self.result = result
            self.url = url
            self.didFallback = didFallback

        @property
        def Result(self):
            return self.result

        @property
        def Url(self):
            return self.url

        @property
        def DidFallback(self):
            return self.didFallback

    # Handles making all http calls out of the plugin to OctoPrint or other services running locally on the device or
    # even on other devices on the LAN.
    #
    # The main point of this function is to abstract away the logic around relative paths, absolute URLs, and the fallback logic
    # we use for different ports. See the comments in the function for details.
    @staticmethod
    def MakeHttpCall(logger, httpInitialContext, method, headers, data=None, stream=False):

        # First of all, we need to figure out what the URL is. There are two options
        #
        # 1) Absolute URLs
        # These are the easiest, because we just want to make a request to exactly what the abolute URL is. These are used
        # when the OctoPrint portal is trying to make an local LAN http request to the same device or even a different device.
        # For these to work properly on a remote browser, the OctoEverywhere service will detect and convert the URLs in to encoded relative
        # URLs for the portal. This ensures when the remote browser tries to access the HTTP endpoint, it will hit OctoEverywhere. The OctoEverywere
        # server detects the special relative URL, decodes the abolute URL, and sends that in the OctoMessage as "AbsUrl". For these URLs we just try
        # to hit them and we take whatever we get, we don't care if fails or not.
        #
        # 2) Relative Urls
        # These Urls are the most common, standard URLs. The browser makes the relative requests to the same hostname:port as it's currently
        # on. However, for our setup its a little more complex. The issue is the OctoEverywhere plugin not knowing how the user's system is setup.
        # The plugin can with 100% certainty query and know the port OctoPrint's http server is running on directly. So we do that to know exactly what
        # OctoPrint server to talk to. (consider there might be multiple instances running on one device.)
        #
        # But, the other most common use case for http calls are the webcam streams to mjpegstreamer. This is the tricky part. There are two ways it can be
        # setup. 1) the webcam stream uses an absolute local LAN url with the ip and port. This is coverted by the abolute URL system above. 2) The webcam stream
        # uses a relative URL and haproxy handles detecting the webcam path to send it to the proper mjpegstreamer instance. This is the tricky one, because we can't
        # directly query or know what the correct port for haproxy or mjpegstreamer is. We could look at the configs, but a user might not setup the configs in the
        # standard places. So to fix the issue, we use logic in the frontend JS to determin if a web browser is connecting locally, and if so what the port is. That gives
        # use a reliable way to know what port haproxy is running on. It sends that to the plugin, which is then given here as `localHttpProxyPort`.
        #
        # The last problem is knowing which calls should be sent to OctoPrint directly and which should be sent to haproxy. We can't rely on any URL matching, because
        # the user can setup the webcam stream to start with anything they want. So the method we use right now is to simply always request to OctoPrint first, and if we
        # get a 404 back try the haproxy. This adds a little bit of unneeded overhead, but it works really well to cover all of the cases.

        # Figure out the main and fallback url.
        url = ""
        fallbackUrl = None
        fallbackWebcamUrl = None
        fallbackLocalIpSuffix = None

        # Get the path var, this is common between both relative and absolute paths.
        path = OctoStreamMsgBuilder.BytesToString(httpInitialContext.Path())
        if path is None:
            raise Exception("Http request has no path field in open message.")

        pathType = httpInitialContext.PathType()
        if pathType == PathTypes.Relative:

            # The main URL is directly to this OctoPrint instance
            # This URL will only every be http, it can't be https.
            url = "http://" + OctoHttpRequest.LocalHostAddress + ":" + str(OctoHttpRequest.LocalOctoPrintPort) + path

            # The fallback URL is to where we think the http proxy port is.
            # For this address, we need set the protocol correctly depending if the client detected https
            # or not.
            protocol = "http://"
            if OctoHttpRequest.LocalHttpProxyIsHttps:
                protocol = "https://"
            fallbackUrl = protocol + OctoHttpRequest.LocalHostAddress + ":" +str(OctoHttpRequest.LocalHttpProxyPort) + path

            # If the two URLs above don't work, we will try to call the server using the local IP since the server might not be bound to localhost.
            # Note we only build the suffix part of the string here, because we don't want to do the local IP detection if we don't have to.
            # Also note this will only work for OctoPrint pages.
            # This case only seems to apply to OctoPrint instances running on Windows.
            fallbackLocalIpSuffix = ":" + str(OctoHttpRequest.LocalOctoPrintPort) + path

            # If all else fails, and because this logic isn't perfect, yet, we will also try to fallback to the assumed webcam port.
            # This isn't a great thing though, because more complex webcam setups use different ports and more than one instance.
            # Only setup this URL if the path starts with /webcam, which again isn't a great indicator because it can change per user.
            webcamUrlIndicator = "/webcam"
            pathLower = path.lower()
            if pathLower.startswith(webcamUrlIndicator):
                # We need to remove the /webcam* since we are trying to talk directly to mjpg-streamer
                # We do want to keep the second / though.
                secondSlash = path.index("/", 1)
                if secondSlash != -1:
                    webcamPath = path[secondSlash:]
                    fallbackWebcamUrl = protocol + OctoHttpRequest.LocalHostAddress + ":8080" + webcamPath

        elif pathType == PathTypes.Absolute:
            # For absolute URLs, only use the main URL and set it be exactly what
            # was requested.
            url = path
        else:
            raise Exception("Http request got a message with an unknown path type. "+str(pathType))

        # Ensure if there's no data we don't set it. Sometimes our json message parsing will leave an empty
        # bytearray where it should be None.
        if data is not None and len(data) == 0:
            data = None

        # First, try the main URL.
        # For the first main url, we set the main response to None and is fallback to False.
        ret = OctoHttpRequest.MakeHttpCallAttempt(logger, "Main request", method, url, headers, data, stream, None, False, fallbackUrl)
        # If the function reports the chain is done, the next fallback URL is invlaid and we should always return
        # whatever is in the Response, even if it's None.
        if ret.IsChainDone:
            return ret.Result

        # We keep track of the main response, if all future fallbacks fail. (This can be None)
        mainResult = ret.Result

        # Main failed, try the fallback, which should be the http proxy.
        ret = OctoHttpRequest.MakeHttpCallAttempt(logger, "Http proxy fallback", method, fallbackUrl, headers, data, stream, mainResult, True, fallbackLocalIpSuffix)
        # If the function reports the chain is done, the next fallback URL is invlaid and we should always return
        # whatever is in the Response, even if it's None.
        if ret.IsChainDone:
            return ret.Result

        # Try the local IP, because the server might not be bound to 127.0.0.1
        localIpFallbackUrl = "http://" + LocalIpHelper.TryToGetLocalIp() + fallbackLocalIpSuffix
        ret = OctoHttpRequest.MakeHttpCallAttempt(logger, "Local IP fallback", method, localIpFallbackUrl, headers, data, stream, mainResult, True, fallbackWebcamUrl)
        # If the function reports the chain is done, the next fallback URL is invlaid and we should always return
        # whatever is in the Response, even if it's None.
        if ret.IsChainDone:
            return ret.Result

        # If all others fail, try the hardcoded webcam URL.
        # Note this has to be last, because there commonly isn't a fallbackWebcamUrl, so it will stop the
        # chain of other attempts.
        ret = OctoHttpRequest.MakeHttpCallAttempt(logger, "Webcam hardcode fallback", method, fallbackWebcamUrl, headers, data, stream, mainResult, True, None)
        # No matter what, always return the result now.
        return ret.Result

    # Returned by a single http request attempt.
    # IsChainDone - indicates if the fallback chain is done and the response should be returned
    # Result - is the final result. Note the result can be unsuccessful or even `None` if everything failed.
    class AttemptResult():
        def __init__(self, isChainDone, result):
            self.isChainDone = isChainDone
            self.result = result

        @property
        def IsChainDone(self):
            return self.isChainDone

        @property
        def Result(self):
            return self.result

    # This function should always return a AttemptResult object.
    @staticmethod
    def MakeHttpCallAttempt(logger, attemptName, method, url, headers, data, stream, mainResult, isFallback, nextFallbackUrl):
        response = None
        try:
            # Try to make the http call.
            #
            # Note we use a long timeout because some api calls can hang for a while.
            # For example when plugins are installed, some have to compile which can take some time.
            #
            # Also note we want to disable redirects. Since we are proxying the http calls, we want to send
            # the redirect back to the client so it can handle it. Otherwise we will return the redirected content
            # for this url, which is incorrect. The X-Forwarded-Host header will tell the OctoPrint server the correct
            # place to set the location redirect header.
            #
            # It's important to set the `verify` = False, since if the server is using SSL it's probally a self-signed cert.
            response = requests.request(method, url, headers=headers, data=data, timeout=1800, allow_redirects=False, stream=stream, verify=False)
        except Exception as e:
            logger.error(attemptName + " http URL threw an exception: "+str(e))

        # Check if we got a valid response.
        if response is not None and response.status_code != 404:
            # We got a valid response, we are done.
            # Return true and the result object, so it can be returned.
            return OctoHttpRequest.AttemptResult(True, OctoHttpRequest.Result(response, url, isFallback))

        # Check if we have another fallback URL to try.
        if nextFallbackUrl is not None:
            # We have more fallbacks to try.
            # Return false so we keep going, but also return this response if we had one. This lets
            # use capture the main result object, so we can use it eventually if all fallbacks fail.
            return OctoHttpRequest.AttemptResult(False, OctoHttpRequest.Result(response, url, isFallback))

        # We don't have another fallback, so we need to end this.
        if mainResult is not None:
            # If we got something back from the main try, always return it (we should only get here on a 404)
            logger.info(attemptName + " failed and we have no more fallbacks. Returning the main URL response.")
            return OctoHttpRequest.AttemptResult(True, mainResult)
        else:
            # Otherwise return the failure.
            logger.error(attemptName + " failed and we have no more fallbacks. We DON'T have a main response.")
            return OctoHttpRequest.AttemptResult(True, None)
