import logging
from typing import Optional

from octoeverywhere.mqttrelay import IMqttRelayUpstream, MqttRelay
from octoeverywhere.sentry import Sentry

from .bambuclient import BambuClient


class BambuMqttRelayUpstream(IMqttRelayUpstream):
    """Bambu-specific implementation of IMqttRelayUpstream.

    Forwards messages published by LAN clients (e.g. Bambu Studio, Home Assistant)
    to the Bambu printer via the existing BambuClient MQTT connection.
    """

    def __init__(self, logger: logging.Logger, bambuClient: BambuClient) -> None:
        self.Logger = logger
        self.BambuClient = bambuClient

    def RelayPublish(self, topic: str, payload: bytes) -> bool:
        """Publish a message from a LAN client to the Bambu printer."""
        return self.BambuClient.PublishRaw(topic, payload)


def StartBambuMqttRelay(logger: logging.Logger, bambuClient: BambuClient, port: int = 1883) -> Optional[MqttRelay]:
    """Create and start the MQTT relay broker for Bambu printers.

    The relay enforces the same auth as the Bambu printer: username 'bblp' and
    the printer's LAN access code. LAN clients must use these credentials to connect.

    Returns the running MqttRelay instance, or None if startup failed.
    """
    try:
        upstream = BambuMqttRelayUpstream(logger, bambuClient)
        # Enforce the same auth the Bambu printer uses: username=bblp, password=LAN access code.
        accessCode = bambuClient.LanAccessCode
        relay = MqttRelay(
            logger, upstream, port=port,
            authUsername="bblp",
            authPassword=accessCode if accessCode is not None else ""
        )
        bambuClient.SetMqttRelay(relay)
        relay.Start()
        return relay
    except Exception as e:
        Sentry.OnException("Failed to start Bambu MQTT relay.", e)
        return None
