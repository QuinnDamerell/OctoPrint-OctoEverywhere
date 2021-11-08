from octoprint_octoeverywhere.octostreammsgbuilder import OctoStreamMsgBuilder
from ..octohttprequest import OctoHttpRequest

class HeaderHelper:

    @staticmethod
    def GatherRequestHeaders(httpInitialContext, logger) :

        hostAddress = OctoHttpRequest.GetLocalhostAddress()

        # Get the count of headers in the message.
        sendHeaders = {}
        headersLen = httpInitialContext.HeadersLength()

        # Convert each header and fix them up.
        i = 0
        while i < headersLen:
            # Get the header
            header = httpInitialContext.Headers(i)
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
                # We don't want to send the transfter encoding since it' won't be accurate any longer.
                # If the request was compressed, it will be de-compressed by the server and then we use a different
                # compression system over the wire.
                # If the request was chunked, our system will read the entire message and send it on the wire
                # in multiable stream messages.
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

            # Update any headers we need to for the local call.
            if lowerName == "host" :
                value = hostAddress
            if lowerName == "referer" :
                value = "http://" + hostAddress
            if lowerName == "origin" :
                value = "http://" + hostAddress

            # Add the header. (use the original case)
            sendHeaders[OctoStreamMsgBuilder.BytesToString(header.Key())] = value

        # The `X-Forwarded-Host` tells the OctoPrint webserver we are talking to what it's actual
        # hostname and port are. This allows it to set outbound urls and references to be correct to the right host.
        # Note! This can do weird things with redirect! Because the redirect location header will actually reflect this
        # hostname. So when your doing local testing, this host name must be correct from the service or incorrect redirects
        # will happen.
        octoHostBytes = httpInitialContext.OctoHost()
        if octoHostBytes is None:
            raise Exception("Http headers found no OctoHost in http initial context.")
        sendHeaders["X-Forwarded-Host"] = OctoStreamMsgBuilder.BytesToString(octoHostBytes)

        # This tells the OctoPrint webserver the client is connected to the proxy via https.
        # At the moment I don't think this matters, but it's the right thing to do.
        sendHeaders["X-Forwarded-Proto"] = "https"

        # We exclude this from being set above, but even more so, we want to define it as empty.
        # If we exclude it, the py request lib seems to add it by itself.
        # We don't want to mess with encoding, because doing to encoding over local host is a waste of time.
        # Unless we can get the already encoded bytes out of the request client, then that might be intereting.
        sendHeaders["Accept-Encoding"] = ""

        return sendHeaders
