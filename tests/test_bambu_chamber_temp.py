import unittest
from typing import Any, Optional

from tests.test_dependency_stubs import InstallTestDependencyStubs

InstallTestDependencyStubs()

# pylint: disable=wrong-import-position,protected-access
from bambu_octoeverywhere.bambuclient import BambuClient  # noqa: E402
from bambu_octoeverywhere.bambucommandhandler import BambuCommandHandler  # noqa: E402
from bambu_octoeverywhere.bambumodels import BambuPrinters, c_BambuModelsWithChamberSensor  # noqa: E402


class _FakeVersion:
    def __init__(self, printerName:Optional[BambuPrinters]) -> None:
        self.PrinterName = printerName


class _FakeClient:
    def __init__(self, version:Any) -> None:
        self._version = version

    def GetVersion(self) -> Any:
        return self._version


class _FakeState:
    def __init__(self, chamberTemper:Optional[float]) -> None:
        self.chamber_temper = chamberTemper


class TestBambuChamberTemp(unittest.TestCase):
    def setUp(self) -> None:
        self._originalInstance = BambuClient._Instance
        self.Handler = BambuCommandHandler.__new__(BambuCommandHandler)

    def tearDown(self) -> None:
        BambuClient._Instance = self._originalInstance

    def _GetTemp(self, model:Optional[BambuPrinters], chamberTemper:Optional[float], version:Any="use-model") -> Optional[float]:
        BambuClient._Instance = _FakeClient(_FakeVersion(model) if version == "use-model" else version) #pyright: ignore[reportAttributeAccessIssue]
        return self.Handler._GetChamberTempOrNone(_FakeState(chamberTemper)) #pyright: ignore[reportArgumentType]

    # These models have a real chamber thermistor, so their reading is reported.
    def test_models_with_a_sensor_report_the_temp(self) -> None:
        for model in (BambuPrinters.X1C, BambuPrinters.X1E, BambuPrinters.H2D, BambuPrinters.H2S):
            self.assertEqual(self._GetTemp(model, 31.5), 31.5, model)

    # These models have no chamber sensor. The firmware still sends a chamber_temper value, but nothing is
    # measuring it, so reporting it would show the user a temperature that isn't real.
    def test_models_without_a_sensor_report_nothing(self) -> None:
        for model in (BambuPrinters.P1S, BambuPrinters.P1P, BambuPrinters.P2S, BambuPrinters.A1, BambuPrinters.A1Mini):
            self.assertIsNone(self._GetTemp(model, 5.0), model)

    # Guessing wrong would show a made up temperature, so an unknown model reports nothing.
    def test_unknown_model_reports_nothing(self) -> None:
        self.assertIsNone(self._GetTemp(BambuPrinters.Unknown, 31.5))
        self.assertIsNone(self._GetTemp(None, 31.5))

    def test_no_version_yet_reports_nothing(self) -> None:
        self.assertIsNone(self._GetTemp(None, 31.5, version=None))

    # A printer that has a sensor but hasn't sent a reading must still report nothing rather than zero.
    def test_missing_reading_reports_nothing(self) -> None:
        self.assertIsNone(self._GetTemp(BambuPrinters.X1C, None))

    # A real zero reading must survive, since it's a value not an absence.
    def test_zero_is_a_real_reading(self) -> None:
        self.assertEqual(self._GetTemp(BambuPrinters.X1C, 0.0), 0.0)

    def test_sensor_model_set_is_what_we_expect(self) -> None:
        self.assertEqual(c_BambuModelsWithChamberSensor,
                         {BambuPrinters.X1C, BambuPrinters.X1E, BambuPrinters.H2D, BambuPrinters.H2S})


if __name__ == "__main__":
    unittest.main()
