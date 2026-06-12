import logging
import unittest

from tests.test_dependency_stubs import InstallTestDependencyStubs

InstallTestDependencyStubs()

from bambu_octoeverywhere.bambumodels import BambuPrintErrors, BambuState  # noqa: E402
from bambu_octoeverywhere.bambustatetranslater import BambuStateTranslator  # noqa: E402


class TestBambuModels(unittest.TestCase):
    def test_hex_error_codes_are_uppercase_for_lookup_and_notifications(self) -> None:
        state = BambuState()
        state.print_error = 0x07FF8011

        self.assertEqual(state.GetPrinterErrorType(), BambuPrintErrors.FilamentRunOut)
        self.assertNotEqual(state.GetDetailedPrinterErrorStr(), "Error")

        translator = BambuStateTranslator(logging.getLogger("TestBambuModels"))
        self.assertEqual(translator._GetBambuPlatformErrorCode(state), "07FF8011")


if __name__ == "__main__":
    unittest.main()
