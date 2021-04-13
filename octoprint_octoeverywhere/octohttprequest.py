import requests

class OctoHttpRequest:
    LocalHttpProxyPort = 80
    LocalOctoPrintPort = 5000
    LocalHostAddress = "127.0.0.1"

    @staticmethod
    def SetLocalHttpProxyPort(port):
        OctoHttpRequest.LocalHttpProxyPort = port

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
    def MakeHttpCall(logger, msg, method, headers, data=None, stream=False):

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

        if "Path" in msg and msg["Path"] != None and len(msg["Path"]) > 0:            
            # If we have the Path var, it means the http request is relative to this device.
            path = msg["Path"]

            # The main URL is directly to this OctoPrint instance
            url = "http://" + OctoHttpRequest.LocalHostAddress + ":" + str(OctoHttpRequest.LocalOctoPrintPort) + path

            # The fallback URL is to where we think the http proxy port is.
            fallbackUrl = "http://" + OctoHttpRequest.LocalHostAddress + ":" +str(OctoHttpRequest.LocalHttpProxyPort) + path

        elif "AbsUrl" in msg and len(msg["AbsUrl"]) > 0:
            # For absolute URLs, only use the main URL and set it be exactly what
            # was requested.
            url = msg["AbsUrl"]
        else:
            logger.error("Http request got a message without a Path or AbsUrl variable.")
            return None

        # Ensure if there's no data we don't set it. Sometimes our json message parsing will leave an empty
        # bytearray where it should be None.
        if data != None and len(data) == 0:
            data = None

        # Try to make the http call.
        # Note we use a long timeout because some api calls can hang for a while.
        # For example when plugins are installed, some have to compile which can take some time.
        # Also note we want to disable redirects. Since we are proxying the http calls, we want to send
        # the redirect back to the client so it can handle it. Otherwise we will return the redirected content
        # for this url, which is incorrect. The X-Forwarded-Host header will tell the OctoPrint server the correct
        # place to set the location redirect header.
        mainResponse = None
        try:
            # Try the main URL
            mainResponse = requests.request(method, url, headers=headers, data=data, timeout=1800, allow_redirects=False, stream=stream)
        except Exception:
            # If we fail, we want to try the fallback, if there is one.
            pass

        # Check if we got a valid response, if so we are done.
        if mainResponse != None and mainResponse.status_code != 404:
            return OctoHttpRequest.Result(mainResponse, url, False)

        # Check if we have a fallback we can try
        if fallbackUrl == None:
            if mainResponse != None:
                # If we got something back, always return it (we should only get here on a 404)
                return OctoHttpRequest.Result(mainResponse, url, False)
            else:
                # Otherwise return the failure.
                return None

        #
        # If we get here, the main response is None or 404 and we have a valid fallback to try.
        fallbackResponse = None
        try:
            fallbackResponse = requests.request(method, fallbackUrl, headers=headers, data=data, timeout=1800, allow_redirects=False, stream=stream)
        except Exception:
            pass

        # If the fallback response failed OR the fallback response is also a 404, return either the main response (if we have one)
        # or Nothing.
        # We want to use the OctoPrint 404 over the fallback's if possible, since it might be expected for OctoPrint and it 
        # could be looking for some header.
        if fallbackResponse == None or fallbackResponse.status_code == 404:
            if mainResponse != None:
                # If we got something back, always return it (we should only get here on a 404)
                return OctoHttpRequest.Result(mainResponse, url, False)
            else:
                # Otherwise return the failure.
                return None

        # If we are here, return the fallback response
        return OctoHttpRequest.Result(fallbackResponse, fallbackUrl, True)