import logging

from ..sentry import Sentry
from ..octostreammsgbuilder import OctoStreamMsgBuilder
from ..octohttprequest import OctoHttpRequest

# Indicates the base protocol, not if it's secure or not.
class BaseProtocol:
    Http = 1
    WebSocket = 2


class HeaderHelper:

    c_xForwardedForProtoHeaderName = "X-Forwarded-Proto"
    c_xForwardedForHostHeaderName = "X-Forwarded-Host"

    # Called by slipstream and the main http class to gather and add required headers.
    @staticmethod
    def GatherRequestHeaders(logger, httpInitialContextOptional, protocol) :

        hostAddress = OctoHttpRequest.GetLocalhostAddress()

        # Get the count of headers in the message.
        sendHeaders = {}
        if httpInitialContextOptional is not None:
            headersLen = httpInitialContextOptional.HeadersLength()
            # Convert each header and fix them up.
            i = 0
            while i < headersLen:
                # Get the header
                header = httpInitialContextOptional.Headers(i)
                i += 1

                # Get the values & validate
                name = OctoStreamMsgBuilder.BytesToString(header.Key())
                value = OctoStreamMsgBuilder.BytesToString(header.Value())
                if name is None or value is None:
                    logger.warn("GatherRequestHeaders found a header that has a null name or value.")
                    continue
                lowerName = name.lower()

                # Filter out headers we don't want to send.
                if lowerName == "accept-encoding":
                    # We don't want to accept encoding because it's just a waste of CPU to send over
                    # local host. We will do our own encoding when we send the data over the websocket.
                    continue
                if lowerName == "transfer-encoding":
                    # We don't want to send the transfer encoding since it' won't be accurate any longer.
                    # If the request was compressed, it will be de-compressed by the server and then we use a different
                    # compression system over the wire.
                    # If the request was chunked, our system will read the entire message and send it on the wire
                    # in multiple stream messages.
                    # Thus, we don't need to / shouldn't include this header.
                    continue
                if lowerName == "upgrade-insecure-requests":
                    # We don't support https over the local host.
                    continue
                if lowerName == "x-forwarded-for":
                    # We should never send these to OctoPrint, or it will detect the IP as external and show
                    # the external connection warning.
                    continue
                if lowerName == "x-real-ip":
                    # We should never send these to OctoPrint, or it will detect the IP as external and show
                    # the external connection warning.
                    continue
                if lowerName == "x-original-proto":
                    # There's no need to send this as well.
                    continue

                # Update any headers we need to for the local call.
                if lowerName == "host" :
                    value = hostAddress
                if lowerName == "referer" :
                    value = "http://" + hostAddress
                if lowerName == "origin" :
                    value = "http://" + hostAddress

                # Add the header. (use the original case)
                sendHeaders[OctoStreamMsgBuilder.BytesToString(header.Key())] = value

        # The `X-Forwarded-Host` tells the OctoPrint web server we are talking to what it's actual
        # hostname and port are. This allows it to set outbound urls and references to be correct to the right host.
        # Note! This can do weird things with redirect! Because the redirect location header will actually reflect this
        # hostname. So when your doing local testing, this host name must be correct from the service or incorrect redirects
        # will happen.
        #
        # Note that the function CorrectLocationResponseHeaderIfNeeded below depends upon this header!
        if httpInitialContextOptional is not None:
            octoHostBytes = httpInitialContextOptional.OctoHost()
            if octoHostBytes is None:
                raise Exception("Http headers found no OctoHost in http initial context.")
            sendHeaders[HeaderHelper.c_xForwardedForHostHeaderName] = OctoStreamMsgBuilder.BytesToString(octoHostBytes)

        # This tells the OctoPrint web server the client is connected to the proxy via the proper protocol.
        # Since this is our service, it will always be secure (https or wss)
        #
        # Note that the function CorrectLocationResponseHeaderIfNeeded below depends upon this header!
        if protocol == BaseProtocol.Http:
            sendHeaders[HeaderHelper.c_xForwardedForProtoHeaderName] = "https"
        elif protocol == BaseProtocol.WebSocket:
            sendHeaders[HeaderHelper.c_xForwardedForProtoHeaderName] = "wss"
        else:
            logger.error("GatherRequestHeaders was sent a protocol it doesn't know! "+str(protocol))

        # We exclude this from being set above, but even more so, we want to define it as empty.
        # If we exclude it, the py request lib seems to add it by itself.
        # We don't want to mess with encoding, because doing to encoding over local host is a waste of time.
        #
        # Note this header is also force set in MakeHttpCall, because calls to things like camera-streamer must set it
        # and no users of the MakeHttpCall support handing response compression.
        sendHeaders["Accept-Encoding"] = "identity"

        return sendHeaders

    # Called only for websockets to get headers.
    @staticmethod
    def GatherWebsocketRequestHeaders(logger:logging.Logger, httpInitialContext) -> dict:
        # Get the count of headers in the message.
        headersLen = httpInitialContext.HeadersLength()

        i = 0
        sendHeaders = {}
        while i < headersLen:
            # Get the header
            header = httpInitialContext.Headers(i)
            i += 1

            # Get the values & validate
            name = OctoStreamMsgBuilder.BytesToString(header.Key())
            value = OctoStreamMsgBuilder.BytesToString(header.Value())
            if name is None or value is None:
                logger.warn("GatherWebsocketRequestHeaders found a header that has a null name or value.")
                continue
            lowerName = name.lower()

            # Right now we only allow a subset of headers. Some headers seem to break the websocket servers, so we only allow the ones
            # we know we need.
            if lowerName.startswith("x-api-key"):
                sendHeaders[name] = value
            elif lowerName == "cookie":
                sendHeaders[name] = value

        return sendHeaders


    # Given an httpInitialContext returns if there are any web socket subprotocols being asked for.
    @staticmethod
    def GetWebSocketSubProtocols(logger:logging.Logger, httpInitialContext) -> list:
        # Get the count of headers in the message.
        headersLen = httpInitialContext.HeadersLength()
        i = 0
        while i < headersLen:
            # Get the header
            header = httpInitialContext.Headers(i)
            i += 1

            # Check if it's the protocol headers\
            name = OctoStreamMsgBuilder.BytesToString(header.Key())
            lowerName = name.lower()
            if lowerName == "sec-websocket-protocol":
                valueList = OctoStreamMsgBuilder.BytesToString(header.Value())
                return valueList.split(",")
        return None


    # We have noticed that some proxy servers aren't setup correctly to forward the x-forwarded-for and such headers.
    # So when the web server responds back with a 301 or 302, the location header might not have the correct hostname, instead an ip like 127.0.0.1.
    #
    # This function must return the location value string again, either corrected or not.
    @staticmethod
    def CorrectLocationResponseHeaderIfNeeded(logger:logging.Logger, requestUri:str, locationValue:str, sendHeaders):
        # The sendHeaders is an dict that was generated by GatherRequestHeaders and were used to send the request.

        # Make sure the location is http(s) or ws(s), since that's all we deal with right now.
        if locationValue.lower().startswith("http") is False and locationValue.lower().startswith("ws"):
            logger.warn("CorrectLocationResponseHeaderIfNeeded got a location string that wasn't http(s) or ws(s). "+locationValue)
            return locationValue

        # Check if we have a X-Forwarded-Host. If we don't, we can't do anything, because we don't know the host to replace.
        if (HeaderHelper.c_xForwardedForHostHeaderName in sendHeaders) is False:
            logger.warn("CorrectLocationResponseHeaderIfNeeded got a location header, but no X-Forwarded-Host header was set.")
            return locationValue
        # Check if we have a X-Forwarded-Proto. If we don't, we can't do anything, because we don't know the proto to replace.
        if (HeaderHelper.c_xForwardedForProtoHeaderName in sendHeaders) is False:
            logger.warn("CorrectLocationResponseHeaderIfNeeded got a location header, but no X-Forwarded-Proto header was set.")
            return locationValue

        # Build what the start of the URL should be.
        # Ex https://test.octoeverywhere.com
        # Note, there should be no trailing /
        urlStart = sendHeaders[HeaderHelper.c_xForwardedForProtoHeaderName] + "://" + sendHeaders[HeaderHelper.c_xForwardedForHostHeaderName]

        try:
            # Parse the existing URL to get the path.
            # pylint: disable=import-outside-toplevel
            from urllib.parse import urlparse
            r = urlparse(locationValue)

            # If the redirect starts with ./ it's referencing the current uri path.
            # For example, if the request uri was https://test.com/hello/world and the redirect is ./overhere?test=1
            # The correct URI is https://test.com/hello/world/overhere?test=1
            path = r.path
            if path.startswith("./"):
                # Parse the request uri to pull the path out.
                ogUri = urlparse(requestUri)
                path = ogUri.path
                # Ensure the path starts with a /
                if path.startswith("/") is False:
                    path += "/"
                # Append the redirect path, but not the ./
                if len(r.path) > 2:
                    path += r.path[2:]

            # Return the new URL
            # The path value will start with a / if there was one in the original path.
            # If there was no slash (http://octoeverywhere.com) path is an empty string.
            # If there is no query string, it's an empty string as well.
            correctedUrl = urlStart + r.path
            correctedUrl = urlStart + path
            if len(r.query) > 0:
                correctedUrl += "?" + r.query

            logger.info("We corrected a response location header "+locationValue+" -> "+correctedUrl)
            return correctedUrl

        except Exception as e:
            Sentry.Exception("CorrectLocationResponseHeaderIfNeeded failed to parse location url "+locationValue, e)
            return locationValue
