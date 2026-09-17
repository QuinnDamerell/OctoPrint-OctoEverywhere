import unittest

from moonraker_octoeverywhere.printerstatemapping import PrinterStateMapping
from moonraker_octoeverywhere.printeradapters import StandardMoonrakerPrinterAdapter


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


    def test_u1_shutdown_json_prefix_is_decoded_before_trailing_text(self) -> None:
        code, error = PrinterStateMapping.GetWebhooksErrorInfo(
            "shutdown",
            '{"coded":"0003-0522-0000-0002","oneshot":0,"msg":"Heater is not heating"}\n'
            "MCU 'toolhead' shutdown: Timeout\n"
            "Once the underlying issue is corrected, restart the printer.")
        self.assertEqual(code, "0003-0522-0000-0002")
        self.assertEqual(error, "Heater is not heating")


    def test_shutdown_envelope_without_detail_uses_actionable_trailing_text(self) -> None:
        code, error = PrinterStateMapping.GetWebhooksErrorInfo(
            "shutdown",
            '{"coded":"0003-0522-0000-0002","msg":""}\n'
            "MCU 'toolhead' shutdown: Timeout\n"
            "Once the underlying issue is corrected, restart the printer.")
        self.assertEqual(code, "0003-0522-0000-0002")
        self.assertEqual(error, "MCU 'toolhead' shutdown: Timeout")


    def test_shutdown_code_without_detail_uses_shutdown_fallback(self) -> None:
        code, error = PrinterStateMapping.GetWebhooksErrorInfo(
            "SHUTDOWN",
            '{"coded":"0003-0522-0000-0002"}\n'
            "Once the underlying issue is corrected, restart the printer.")
        self.assertEqual(code, "0003-0522-0000-0002")
        self.assertEqual(error, "Klipper Shutdown")


    def test_same_encoded_error_is_consistent_for_print_and_shutdown(self) -> None:
        message = '{"coded":"0003-0522-0000-0002","msg":"Printer stopped"}\nRaw shutdown detail'
        self.assertEqual(
            PrinterStateMapping.GetPrintStatsErrorInfo({"state": "error", "message": message}),
            PrinterStateMapping.GetWebhooksErrorInfo("shutdown", message))


    def test_structured_exception_message_can_use_code_from_encoded_message(self) -> None:
        code, error = PrinterStateMapping.GetPrintStatsErrorInfo({
            "state": "error",
            "exception": {"message": "Clean detail"},
            "message": '{"coded":"0003-0522-0000-0002","msg":"Raw detail"}',
        })
        self.assertEqual(code, "0003-0522-0000-0002")
        self.assertEqual(error, "Clean detail")


    def test_u1_tuple_is_recognized_inside_nested_errors_without_discovery(self) -> None:
        code, error = PrinterStateMapping.GetWebhooksErrorInfo(
            "shutdown", '{"error":{"level":3,"id":522,"index":0,"code":2,"message":"Tool failed"}}')
        self.assertEqual(code, "0003-0522-0000-0002")
        self.assertEqual(error, "Tool failed")


    def test_malformed_json_preserves_original_error(self) -> None:
        message = '{"coded":"0003-0522-0000-0002","msg":"Bad data"'
        self.assertEqual(PrinterStateMapping.GetWebhooksErrorInfo("error", message), ("klippy_error", message))


    def test_unknown_json_preserves_diagnostic_information(self) -> None:
        message = '{"unexpected_firmware_field":"Something failed"}'
        self.assertEqual(PrinterStateMapping.GetWebhooksErrorInfo("error", message), ("klippy_error", message))


    def test_generic_shutdown_json_is_supported_by_standard_adapter(self) -> None:
        code, error = PrinterStateMapping.GetWebhooksErrorInfo(
            "shutdown", '{"error_code":"HEATER-2","description":"Heater timeout"}',
            adapter=StandardMoonrakerPrinterAdapter())
        self.assertEqual(code, "HEATER-2")
        self.assertEqual(error, "Heater timeout")


    def test_ready_state_clears_previous_shutdown_error(self) -> None:
        self.assertEqual(
            PrinterStateMapping.GetWebhooksErrorInfo("ready", '{"coded":"0003-0522-0000-0002","msg":"Old error"}'),
            (None, None))


    def test_u1_zero_exception_does_not_report_a_platform_fault(self) -> None:
        code, error = PrinterStateMapping.GetPrintStatsErrorInfo({
            "state": "printing", "message": "",
            "exception": {"level": 0, "id": 0, "index": 0, "code": 0, "message": ""},
        })
        self.assertEqual(code, "printing")
        self.assertIsNone(error)


    def test_empty_encoded_error_structures_do_not_become_error_text(self) -> None:
        for message in ["{}", "[]", '{"message":""}', '{"coded":"0000-0000-0000-0000","msg":""}']:
            with self.subTest(message=message):
                self.assertEqual(PrinterStateMapping.GetPrintStatsErrorInfo({"state": "paused", "message": message}),
                                 ("paused", None))


    def test_extremely_nested_error_does_not_recurse_forever(self) -> None:
        error = {"message": "Innermost detail"}
        for _ in range(100):
            error = {"error": error}
        self.assertEqual(
            PrinterStateMapping.GetPrintStatsErrorInfo({"state": "error", "error": error}), ("error", None))


    def test_long_shutdown_detail_remains_bounded(self) -> None:
        _, error = PrinterStateMapping.GetWebhooksErrorInfo("shutdown", "X" * 2000)
        self.assertEqual(len(error), 1000)
        self.assertTrue(error.endswith("..."))


if __name__ == "__main__":
    unittest.main()
