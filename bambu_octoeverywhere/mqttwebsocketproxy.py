import logging

from octoeverywhere.mqttmux.relayproxy import MqttRelayWebSocketProxyProviderBuilder


# Bambu's mux is registered in MqttMuxRegistry under the printer SN
# (see BambuClient.__init__). The relay builder looks it up lazily so
# import order doesn't matter.
def _BambuMuxKeyFromConfig():  # noqa: D401 - small helper
    # We avoid importing BambuClient here to dodge circular-import risks at
    # plugin load. The relay builder accepts the key string and resolves it
    # via the registry at connect time.
    from .bambuclient import BambuClient  # pylint: disable=import-outside-toplevel
    instance = BambuClient.Get()
    if instance is None:
        return None
    return instance.PrinterSn


# Keep this Bambu-local name so existing host code can import it unchanged.
# Hosts construct this and hand it to Compat.SetMqttWebsocketProxyProviderBuilder.
class MqttWebsocketProxyProviderBuilder(MqttRelayWebSocketProxyProviderBuilder):

    def __init__(self, logger: logging.Logger) -> None:
        # The mux key is the printer SN; the BambuClient instance must have
        # been constructed before any incoming relay connection.
        key = _BambuMuxKeyFromConfig()
        if key is None:
            # Fall back to an empty key - the registry lookup will fail at
            # connect time and the proxy will reject the relay connection.
            key = ""
        super().__init__(logger, mux_key=key)
