# Respresents the header class we encode in json.
class Header:
    Name = ""
    Value = ""
    def __init__(self, name, value):
        self.Name = name
        self.Value = value

    @staticmethod
    def GatherRequestHeaders(msg, hostAddress) :
        send_headers = {}
        for header in msg["Headers"]:
            lowerName = header["Name"].lower()
            value = header["Value"]

            # Filter out headers we don't want to send.
            if lowerName == "accept-encoding":
                # We don't want to accept encoding because it's just a waste of CPU to send over
                # local host. We will do our own encoding when we send the data over the websocket.
                continue
            if lowerName == "upgrade-insecure-requests":
                # We don't support https over the local host.
                continue 

            # Update any headers we need to for the local call.
            if lowerName == "host" :
                value = hostAddress
            if lowerName == "referer" :
                value = "http://" + hostAddress
            if lowerName == "origin" :
                value = "http://" + hostAddress

            # Add the header. (use the original case)
            send_headers[header["Name"]] = value

        # The `X-Forwarded-Host` tells the OctoPrint webserver we are talking to what it's actual
        # hostname and port are. This allows it to set outbound urls and references to be correct to the right host.
        # Note! This can do weird things with redirect! Because the redirect location header will actually reflect this
        # hostname. So when your doing local testing, this host name must be correct from the service or incorrect redirects
        # will happen.   
        send_headers["X-Forwarded-Host"] = msg["OctoHost"]

        # This tells the OctoPrint webserver the client is connected to the proxy via https.
        # At the moment I don't think this matters, but it's the right thing to do.
        send_headers["X-Forwarded-Proto"] = "https"

        return send_headers