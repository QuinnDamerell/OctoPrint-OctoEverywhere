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
                # We don't want to accept encoding becuase it's just a waste of CPU to send over
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

            # Add the header. (use the orgional case)
            send_headers[header["Name"]] = value
        return send_headers