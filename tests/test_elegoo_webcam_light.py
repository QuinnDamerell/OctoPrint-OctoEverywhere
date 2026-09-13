import logging
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


clientAccessor = MagicMock()


def _Module(name:str, **attrs:object) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


with patch.dict(sys.modules, {
    "elegoo_octoeverywhere.elegooclient": _Module("elegooclient", ElegooClient=clientAccessor),
    "octoeverywhere.Webcam.quickcam": _Module("quickcam", QuickCam=SimpleNamespace(JMPEGProtocol="jmpeg://")),
    "octoeverywhere.Webcam.webcamsettingitem": _Module("webcamsettingitem", WebcamSettingItem=object),
    "octoeverywhere.interfaces": _Module("interfaces", IWebcamPlatformHelper=object),
}):
    from elegoo_octoeverywhere.elegoowebcamhelper import ElegooWebcamHelper


class TestElegooWebcamLight(unittest.TestCase):
    def _StartStream(self, lightState:object, autoActivate:bool=True) -> MagicMock:
        client = MagicMock()
        client.IsWebsocketConnected.return_value = True
        client.GetState.return_value = lightState
        clientAccessor.Get.return_value = client

        config = MagicMock()
        config.GetBool.return_value = autoActivate
        ElegooWebcamHelper(logging.getLogger(__name__), config).OnQuickCamStreamStart("test")
        return client

    def test_sends_light_command_only_when_status_reports_off(self) -> None:
        client = self._StartStream(SimpleNamespace(ChamberLightOn=False))

        client.SendRequest.assert_called_once_with(
            403, {"LightStatus": {"SecondLight": True, "RgbLight": [0, 0, 0]}}, waitForResponse=False)

    def test_skips_light_command_without_an_explicit_off_status(self) -> None:
        cases = [
            (SimpleNamespace(ChamberLightOn=True), True),
            (SimpleNamespace(ChamberLightOn=None), True),
            (None, True),
            (SimpleNamespace(ChamberLightOn=False), False),
        ]
        for state, autoActivate in cases:
            with self.subTest(state=state, autoActivate=autoActivate):
                self._StartStream(state, autoActivate).SendRequest.assert_not_called()


if __name__ == "__main__":
    unittest.main()
