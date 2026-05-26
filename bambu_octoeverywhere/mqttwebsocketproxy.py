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
        # Use a placeholder key; we override GetMuxKey to resolve lazily so
        # the builder can be constructed before BambuClient.Init().
        super().__init__(logger, mux_key="")

    def GetMuxKey(self) -> str:
        key = _BambuMuxKeyFromConfig()
        if key is None:
            return ""
        return key
