import unittest

from moonraker_octoeverywhere.printerstatemapping import PrinterStateMapping


class TestPrinterStateMapping(unittest.TestCase):
    def test_u1_exception_maps_code_and_clean_message(self) -> None:
        code, error = PrinterStateMapping.GetPrintStatsErrorInfo({
            "state": "paused",
            "message": '{"coded":"0002-0525-0003-0011","msg":"Raw message","action":"pause"}',
            "exception": {
                "level": 2,
                "id": 525,
                "index": 3,
                "code": 11,
                "message": "Filament is stuck",
            },
        })

        self.assertEqual(code, "0002-0525-0003-0011")
        self.assertEqual(error, "Filament is stuck")


    def test_encoded_u1_message_is_used_without_exception_object(self) -> None:
        code, error = PrinterStateMapping.GetPrintStatsErrorInfo({
            "state": "error",
            "message": '{"coded":"0003-0522-0000-0002","msg":"Printer stopped","action":"cancel"}',
        })

        self.assertEqual(code, "0003-0522-0000-0002")
        self.assertEqual(error, "Printer stopped")


    def test_generic_moonraker_falls_back_to_state_and_message(self) -> None:
        code, error = PrinterStateMapping.GetPrintStatsErrorInfo({
            "state": "paused",
            "message": "Paused by printer",
        })

        self.assertEqual(code, "paused")
        self.assertEqual(error, "Paused by printer")


    def test_empty_optional_message_is_omitted(self) -> None:
        code, error = PrinterStateMapping.GetPrintStatsErrorInfo({
            "state": "error",
            "message": " ",
            "exception": {},
        })

        self.assertEqual(code, "error")
        self.assertIsNone(error)


    def test_common_vendor_error_aliases_are_parsed(self) -> None:
        code, error = PrinterStateMapping.GetPrintStatsErrorInfo({
            "state": "paused",
            "error": {
                "error_code": "FS-001",
                "reason": "Filament sensor triggered",
            },
        })

        self.assertEqual(code, "FS-001")
        self.assertEqual(error, "Filament sensor triggered")


    def test_pause_can_use_same_update_display_message(self) -> None:
        code, error = PrinterStateMapping.GetPrintStatsErrorInfo(
            {"state": "paused", "message": ""},
            supplementalMessage="Filament runout on tool 1"
        )

        self.assertEqual(code, "paused")
        self.assertEqual(error, "Filament runout on tool 1")


    def test_webhooks_shutdown_message_is_shortened_for_notification(self) -> None:
        code, error = PrinterStateMapping.GetWebhooksErrorInfo(
            "shutdown",
            "MCU 'mcu' shutdown: ADC out of range\n"
            "This generally occurs when a heater temperature exceeds\n"
            "its configured min_temp or max_temp.",
            "notify_klippy_shutdown"
        )

        self.assertEqual(code, "klippy_shutdown")
        self.assertEqual(error, "MCU 'mcu' shutdown: ADC out of range")


    def test_webhooks_disconnect_does_not_reuse_stale_ready_message(self) -> None:
        code, error = PrinterStateMapping.GetWebhooksErrorInfo(
            "ready",
            "Printer is ready",
            "notify_klippy_disconnected"
        )

        self.assertEqual(code, "klippy_disconnected")
        self.assertEqual(error, "Klipper Disconnected")


    def test_webhooks_shutdown_does_not_reuse_stale_ready_message(self) -> None:
        code, error = PrinterStateMapping.GetWebhooksErrorInfo(
            "ready",
            "Printer is ready",
            "notify_klippy_shutdown"
        )

        self.assertEqual(code, "klippy_shutdown")
        self.assertEqual(error, "Klipper Shutdown")


if __name__ == "__main__":
    unittest.main()
