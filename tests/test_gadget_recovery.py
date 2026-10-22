import logging
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests.test_dependency_stubs import InstallTestDependencyStubs

InstallTestDependencyStubs()

from elegoo_octoeverywhere.elegooclient import ElegooClient  # noqa: E402
from elegoo_octoeverywhere.elegoostatetranslater import ElegooStateTranslator  # noqa: E402
from octoeverywhere.gadget import Gadget  # noqa: E402
from octoeverywhere.notificationshandler import NotificationsHandler  # noqa: E402
from octoeverywhere.printinfo import PrintInfoManager  # noqa: E402


class TestGadgetRecovery(unittest.TestCase):
    def setUp(self) -> None:
        self.logger = logging.getLogger("TestGadgetRecovery")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        PrintInfoManager.Init(self.logger, self.tmp.name)


    def _MakeTrackingHandler(self):
        translator = ElegooStateTranslator(self.logger)
        handler = NotificationsHandler(self.logger, translator)
        translator.SetNotificationHandler(handler)
        handler.PrintCookie = "active-print"
        PrintInfoManager.Get().CreateNewPrintInfo(handler.PrintCookie, "original-id")
        handler.ProgressTimer = Mock()
        handler.FirstLayerTimer = Mock()
        handler.PingTimerHoursReported = 4
        handler.Gadget.Profiler = Mock()
        return handler


    @staticmethod
    def _SetGadgetPrintState(gadget):
        gadget.ScoreHistory = [0.1, 0.2]
        gadget.MostRecentGadgetScore = 0.2
        gadget.MostRecentWarningTimeSec = 123
        gadget.MostRecentPauseTimeSec = 456
        gadget.IsSuppressed = True
        gadget.HostLockHostname = "gadget-test"


    def _AssertGadgetPrintState(self, gadget):
        self.assertEqual(gadget.ScoreHistory, [0.1, 0.2])
        self.assertEqual(gadget.MostRecentGadgetScore, 0.2)
        self.assertEqual(gadget.MostRecentWarningTimeSec, 123)
        self.assertEqual(gadget.MostRecentPauseTimeSec, 456)
        self.assertTrue(gadget.IsSuppressed)
        self.assertEqual(gadget.HostLockHostname, "gadget-test")


    def test_disconnected_tick_then_restore_restarts_only_gadget(self) -> None:
        handler = self._MakeTrackingHandler()
        progressTimer = handler.ProgressTimer
        firstLayerTimer = handler.FirstLayerTimer
        client = ElegooClient.__new__(ElegooClient)
        client.State = None
        client.SleepEvent = Mock()
        oldTimer = Mock()
        restoredTimer = Mock()
        with patch("octoeverywhere.gadget.RepeatTimer", side_effect=[oldTimer, restoredTimer]) as timerFactory, \
             patch.object(ElegooClient, "Get", return_value=client), \
             patch("octoeverywhere.gadget.Sentry.OnException") as report:
            handler.Gadget.StartWatching()
            self._SetGadgetPrintState(handler.Gadget)

            # Exercise the real callback and Elegoo state reporter during a disconnect.
            handler.Gadget._timerCallback()
            oldTimer.Stop.assert_called_once()
            self.assertIsNone(handler.Gadget.Timer)
            self.assertIs(handler.ProgressTimer, progressTimer)

            client.State = Mock()
            client.State.IsPrinting.return_value = True
            handler.OnRestorePrintIfNeeded(True, False, "active-print")
            handler.OnRestorePrintIfNeeded(True, False, "active-print")

            self.assertIs(handler.Gadget.Timer, restoredTimer)
            self.assertEqual(timerFactory.call_count, 2)
            restoredTimer.start.assert_called_once()
            restoredTimer.Stop.assert_not_called()
            report.assert_not_called()

        self.assertEqual(handler.GetPrintId(), "original-id")
        self.assertEqual(handler.PrintCookie, "active-print")
        self.assertEqual(handler.PingTimerHoursReported, 4)
        self.assertIs(handler.ProgressTimer, progressTimer)
        self.assertIs(handler.FirstLayerTimer, firstLayerTimer)
        progressTimer.Stop.assert_not_called()
        firstLayerTimer.Stop.assert_not_called()
        self._AssertGadgetPrintState(handler.Gadget)


    def test_repeated_restore_preserves_healthy_gadget(self) -> None:
        handler = self._MakeTrackingHandler()
        with patch("octoeverywhere.gadget.RepeatTimer") as timerFactory:
            handler.Gadget.StartWatching()
            self._SetGadgetPrintState(handler.Gadget)
            timer = handler.Gadget.Timer

            handler.OnRestorePrintIfNeeded(True, False, "active-print")
            handler.OnRestorePrintIfNeeded(True, False, "active-print")

            timerFactory.assert_called_once()
            self.assertIs(handler.Gadget.Timer, timer)
            timer.Stop.assert_not_called()
            timer.SetInterval.assert_not_called()
            self._AssertGadgetPrintState(handler.Gadget)


    def test_restore_during_disconnected_state_check_keeps_gadget_running(self) -> None:
        handler = self._MakeTrackingHandler()
        with patch("octoeverywhere.gadget.RepeatTimer") as timerFactory, \
             patch("octoeverywhere.gadget.Sentry.OnException") as report:
            handler.Gadget.StartWatching()
            self._SetGadgetPrintState(handler.Gadget)
            timer = handler.Gadget.Timer

            def restoreBeforeStateCheckReturns():
                # The state query began while disconnected, but a reconnect restores the active print before it returns.
                handler.OnRestorePrintIfNeeded(True, False, "active-print")
                return False

            with patch.object(handler.PrinterStateInterface, "ShouldPrintingTimersBeRunning", side_effect=restoreBeforeStateCheckReturns):
                handler.Gadget._timerCallback()

            timerFactory.assert_called_once()
            self.assertIs(handler.Gadget.Timer, timer)
            timer.Stop.assert_not_called()
            self._AssertGadgetPrintState(handler.Gadget)
            report.assert_not_called()


    def test_paused_or_inactive_restore_does_not_restart_gadget(self) -> None:
        for isPrinting, isPaused, cookie in [(False, True, "active-print"), (False, False, "active-print"), (True, False, "")]:
            with self.subTest(isPrinting=isPrinting, isPaused=isPaused, cookie=cookie):
                handler = self._MakeTrackingHandler()
                with patch("octoeverywhere.gadget.RepeatTimer") as timerFactory:
                    handler.Gadget.StartWatching()
                    handler.Gadget.StopWatching()
                    handler.OnRestorePrintIfNeeded(isPrinting, isPaused, cookie)
                    handler.OnRestorePrintIfNeeded(isPrinting, isPaused, cookie)
                    timerFactory.assert_called_once()
                    self.assertIsNone(handler.Gadget.Timer)
                    self.assertIsNone(handler.ProgressTimer)


    def test_explicit_start_still_resets_previous_print_state(self) -> None:
        handler = self._MakeTrackingHandler()
        with patch("octoeverywhere.gadget.RepeatTimer", side_effect=[Mock(), Mock()]) as timerFactory:
            handler.Gadget.StartWatching()
            oldTimer = handler.Gadget.Timer
            self._SetGadgetPrintState(handler.Gadget)
            handler.Gadget.StartWatching()

            oldTimer.Stop.assert_called_once()
            self.assertEqual(timerFactory.call_count, 2)
            self.assertEqual(handler.Gadget.ScoreHistory, [])
            self.assertEqual(handler.Gadget.MostRecentGadgetScore, 0.0)
            self.assertFalse(handler.Gadget.IsSuppressed)
            self.assertIsNone(handler.Gadget.HostLockHostname)
            self.assertIsNone(handler.Gadget.MostRecentWarningTimeSec)
            self.assertIsNone(handler.Gadget.MostRecentPauseTimeSec)
            self.assertEqual(handler.Gadget.MostRecentIntervalSec, Gadget.c_defaultIntervalSec)


if __name__ == "__main__":
    unittest.main()
