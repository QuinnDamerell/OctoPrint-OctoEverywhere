import threading
from typing import Dict, Optional

from .mux import MqttUpstreamMux


# Process-wide lookup for MqttUpstreamMux instances, keyed by an opaque
# printer identifier (serial number for Bambu/Elegoo, anything stable for
# other vendors). Lets the WS relay proxy and the local TCP broker resolve
# the same mux that the vendor's plugin client constructed at startup.
#
# Why a registry instead of a per-vendor singleton: a host process may serve
# multiple printers in the future (multi-printer companion modes). Today
# there's only one entry but the lookup-by-key API is forward-compatible.
class MqttMuxRegistry:

    _lock = threading.RLock()
    _muxes: Dict[str, MqttUpstreamMux] = {}


    @staticmethod
    def Register(key: str, mux: MqttUpstreamMux) -> None:
        with MqttMuxRegistry._lock:
            existing = MqttMuxRegistry._muxes.get(key)
            if existing is not None and existing is not mux:
                raise ValueError(f"MqttMuxRegistry already has a different mux for key={key!r}")
            MqttMuxRegistry._muxes[key] = mux


    @staticmethod
    def Unregister(key: str) -> None:
        with MqttMuxRegistry._lock:
            MqttMuxRegistry._muxes.pop(key, None)


    @staticmethod
    def Get(key: str) -> Optional[MqttUpstreamMux]:
        with MqttMuxRegistry._lock:
            return MqttMuxRegistry._muxes.get(key)


    # Returns the single registered mux iff exactly one exists, else None.
    # Convenience for vendor code that knows the process serves one printer.
    @staticmethod
    def GetSole() -> Optional[MqttUpstreamMux]:
        with MqttMuxRegistry._lock:
            if len(MqttMuxRegistry._muxes) == 1:
                return next(iter(MqttMuxRegistry._muxes.values()))
            return None
