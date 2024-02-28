import logging
import ssl
import json
import threading

import paho.mqtt.client as mqtt

from linux_host.config import Config

class BambuClient:

    def __init__(self, logger:logging.Logger, config:Config) -> None:
        self.Logger = logger

        self.IpOrHostname = config.GetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, None)
        self.PortStr  = config.GetStr(Config.SectionCompanion, Config.CompanionKeyPort, None)
        self.AccessToken  = config.GetStr(Config.SectionBambu, Config.BambuAccessToken, None)
        self.PrinterSn  = config.GetStr(Config.SectionBambu, Config.BambuPrinterSn, None)
        if self.IpOrHostname is None or self.PortStr is None or self.AccessToken is None or self.PrinterSn is None:
            raise Exception("Missing required args from the config")

        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self.client.tls_set(tls_version=ssl.PROTOCOL_TLS, cert_reqs=ssl.CERT_NONE)
        self.client.tls_insecure_set(True)
        self.client.username_pw_set("bblp", self.AccessToken)
        self.client.on_connect = self.OnConnect
        self.client.on_message = self.OnMessage
        self.client.on_disconnect = self.OnDisconnect
        self.client.connect(self.IpOrHostname, int(self.PortStr), 60)
        t = threading.Thread(target=self.Worker)
        t.start()


    def DoesNothing(self):
        pass


    def Worker(self):
        self.client.loop_forever()


    def OnConnect(self, client, userdata, flags, reason_code, properties):
        self.Logger.warn("MQTT connected")
        client.subscribe(f"device/{self.PrinterSn}/report")


    def OnDisconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        self.Logger.warn("MQTT disconnected")


    def OnMessage(self, client, userdata, msg):
        try:
            doc = json.loads(msg.payload)
            self.Logger.info("Bambu "+json.dumps(doc, indent=3))
            if doc is None:
                return
            self.client.publish(f"device/{self.PrinterSn}/request", '{{"pushing": {{ "sequence_id": 1, "command": "pushall"}}, "user_id":"1234567890"}}')
        except Exception:
            pass
