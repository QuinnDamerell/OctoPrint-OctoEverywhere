import sys
import types
from enum import IntEnum

try:
    from tests.test_dependency_stubs import InstallTestDependencyStubs
except Exception:
    from test_dependency_stubs import InstallTestDependencyStubs # type: ignore


InstallTestDependencyStubs()


def _InstallModule(name:str) -> types.ModuleType:
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        sys.modules[name] = module
    return module


def _InstallPahoStub() -> None:
    paho = _InstallModule("paho")
    mqttPackage = _InstallModule("paho.mqtt")
    clientModule = _InstallModule("paho.mqtt.client")
    enumsModule = _InstallModule("paho.mqtt.enums")

    class MQTTErrorCode(IntEnum):
        MQTT_ERR_SUCCESS = 0
        MQTT_ERR_NO_CONN = 4

    class CallbackAPIVersion(IntEnum):
        VERSION2 = 2

    class Client:
        def __init__(self, *args, **kwargs) -> None:
            raise RuntimeError("Tests should pass a FakePahoClient via client_factory.")

    clientModule.Client = Client
    clientModule.CallbackAPIVersion = CallbackAPIVersion
    clientModule.MQTT_LOG_ERR = 8
    clientModule.MQTT_LOG_WARNING = 4
    enumsModule.MQTTErrorCode = MQTTErrorCode

    paho.mqtt = mqttPackage
    mqttPackage.client = clientModule
    mqttPackage.enums = enumsModule


_InstallPahoStub()
