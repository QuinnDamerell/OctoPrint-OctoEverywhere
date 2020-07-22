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
            name = header["Name"]
            value = header["Value"]
            if name == "Host" :
                value = hostAddress
            if name == "Referer" :
                value = "http://" + hostAddress
            if name == "Origin" :
                value = "http://" + hostAddress
            if name == "Upgrade-Insecure-Requests":
                continue 
            send_headers[name] = value
        return send_headers