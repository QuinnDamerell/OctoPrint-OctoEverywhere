import logging
import threading
from typing import Optional, Tuple

from octoeverywhere.interfaces import IPrinterStateReporter
from octoeverywhere.notificationshandler import NotificationsHandler
from octoeverywhere.util.delayedcallback import DelayedCallback

from .prusalinkclient import PrusaLinkClient
from .prusalinkmodels import PrinterState
from .interfaces import IStateTranslator


class PrusaLinkStateTranslator(IPrinterStateReporter, IStateTranslator):

    c_ConnectionLostNotificationDelaySec = 10.0

    def __init__(self, logger:logging.Logger) -> None:
        self.Logger = logger
        self.NotificationsHandler:NotificationsHandler = None #pyright: ignore[reportAttributeAccessIssue]
        self.LastStatus:Optional[str] = None
        self.IsWaitingOnPrintInfoToFirePrintStart = False
        self.DelayedConnectionLostCallback:Optional[DelayedCallback] = None
        self.DelayedConnectionLostCallbackLock = threading.Lock()


    def SetNotificationHandler(self, notificationHandler:NotificationsHandler) -> None:
        self.NotificationsHandler = notificationHandler


    def OnConnectionLost(self, wasFullyConnected:bool) -> None:
        if wasFullyConnected and (PrinterState.IsPrepareOrSlicingState(self.LastStatus) or PrinterState.IsPrintingState(self.LastStatus, False)):
            with self.DelayedConnectionLostCallbackLock:
                if self.DelayedConnectionLostCallback is not None:
                    self.DelayedConnectionLostCallback.Cancel()
                self.DelayedConnectionLostCallback = DelayedCallback.Create(self.Logger, "PrusaLinkDelayedConnectionLostCallback", self.c_ConnectionLostNotificationDelaySec, self._DelayedConnectionLostCallback)

        self.LastStatus = None
        self.IsWaitingOnPrintInfoToFirePrintStart = False


    def _DelayedConnectionLostCallback(self) -> None:
        self.NotificationsHandler.OnError("Connection to printer lost during a print.", platformErrorCode="connection_lost")


    def OnStatusUpdate(self, pState:PrinterState, isFirstFullSyncResponse:bool) -> None:
        with self.DelayedConnectionLostCallbackLock:
            if self.DelayedConnectionLostCallback is not None:
                self.DelayedConnectionLostCallback.Cancel()
                self.DelayedConnectionLostCallback = None

        if isFirstFullSyncResponse:
            self.NotificationsHandler.OnRestorePrintIfNeeded(pState.IsPrinting(False), pState.IsPaused(), pState.GetPrintCookie())

        (newStatus, _) = pState.GetCurrentStatus()
        if self.LastStatus != newStatus:
            self.Logger.debug(
                "Prusa Link printer state change: %s -> %s - Printer State: %s Job State: %s",
                self.LastStatus,
                newStatus,
                pState.PrinterState,
                pState.JobState,
            )
            if self.LastStatus is None:
                pass
            elif pState.IsPrinting(False):
                if self.LastStatus == PrinterState.PRINT_STATUS_PAUSED:
                    self.OnResume(pState)
                else:
                    if PrinterState.IsPrintingState(self.LastStatus, False) is False:
                        self.IsWaitingOnPrintInfoToFirePrintStart = True
            elif pState.IsPaused():
                self.OnPause(pState)
            elif newStatus == PrinterState.PRINT_STATUS_CANCELLED:
                self.OnCancelled(pState)
            elif newStatus == PrinterState.PRINT_STATUS_COMPLETE:
                self.OnComplete(pState)
            elif newStatus == PrinterState.PRINT_STATUS_ERROR:
                self.OnError(pState)

            self.LastStatus = newStatus

        if self.IsWaitingOnPrintInfoToFirePrintStart:
            printCookie = pState.GetPrintCookie()
            if printCookie is not None and len(printCookie) > 0:
                self.IsWaitingOnPrintInfoToFirePrintStart = False
                if pState.IsPrinting(True):
                    self.OnStart(pState)

        if not isFirstFullSyncResponse and self.NotificationsHandler.IsTrackingPrint():
            if pState.Progress is not None:
                self.OnPrintProgress(pState)


    def OnStart(self, printerState:PrinterState) -> None:
        fileSizeKb = 0
        if printerState.FileSizeBytes is not None:
            fileSizeKb = int(printerState.FileSizeBytes / 1024)
        filamentUsedMm = printerState.EstFilamentUsedMm if printerState.EstFilamentUsedMm is not None else 0
        filamentWeightMg = printerState.EstFilamentWeightMg if printerState.EstFilamentWeightMg is not None else 0
        self.NotificationsHandler.OnStarted(printerState.GetPrintCookie(), printerState.GetFileNameWithNoExtension(), fileSizeKBytes=fileSizeKb, totalFilamentUsageMm=filamentUsedMm, totalFilamentWeightMg=filamentWeightMg)


    def OnComplete(self, printerState:PrinterState) -> None:
        mostRecentPrint = printerState.GetMostRecentPrintInfo()
        self.NotificationsHandler.OnDone(mostRecentPrint.GetFileNameWithNoExtension(), str(mostRecentPrint.DurationSec))


    def OnPause(self, printerState:PrinterState) -> None:
        mostRecentPrint = printerState.GetMostRecentPrintInfo()
        self.NotificationsHandler.OnPaused(
            mostRecentPrint.GetFileNameWithNoExtension(),
            platformErrorCode=self._GetPrusaLinkPlatformErrorCode(printerState),
            error=self._GetPrusaLinkError(printerState)
        )


    def OnResume(self, printerState:PrinterState) -> None:
        mostRecentPrint = printerState.GetMostRecentPrintInfo()
        self.NotificationsHandler.OnResume(mostRecentPrint.GetFileNameWithNoExtension())


    def OnCancelled(self, printerState:PrinterState) -> None:
        mostRecentPrint = printerState.GetMostRecentPrintInfo()
        self.NotificationsHandler.OnFailed(
            mostRecentPrint.GetFileNameWithNoExtension(),
            None,
            "cancelled",
            platformErrorCode=self._GetPrusaLinkPlatformErrorCode(printerState),
            error=self._GetPrusaLinkError(printerState)
        )


    def OnError(self, printerState:PrinterState) -> None:
        self.NotificationsHandler.OnError(
            self._GetPrusaLinkError(printerState),
            platformErrorCode=self._GetPrusaLinkPlatformErrorCode(printerState)
        )


    def OnPrintProgress(self, printerState:PrinterState) -> None:
        progress = printerState.Progress
        if progress is None:
            progress = 0.0
        self.NotificationsHandler.OnPrintProgress(None, float(progress))


    def _GetPrusaLinkPlatformErrorCode(self, printerState:PrinterState) -> Optional[str]:
        parts = []
        if printerState.PrinterState is not None:
            parts.append("printer_state=" + str(printerState.PrinterState))
        if printerState.JobState is not None:
            parts.append("job_state=" + str(printerState.JobState))
        if len(parts) == 0:
            return None
        return ";".join(parts)


    def _GetPrusaLinkError(self, printerState:PrinterState) -> Optional[str]:
        if printerState.StatusMessage is not None and printerState.StatusMessage.upper() != "OK":
            return printerState.StatusMessage
        if printerState.ConnectMessage is not None and printerState.ConnectMessage.upper() != "OK":
            return printerState.ConnectMessage
        return None


    def GetPrintTimeRemainingEstimateInSeconds(self) -> int:
        state = PrusaLinkClient.Get().GetState()
        if state is None:
            return -1
        timeRemainingSec = state.GetMostRecentPrintInfo().GetTimeRemainingSec()
        if timeRemainingSec is None:
            timeRemainingSec = state.RemainingTimeSec
        if timeRemainingSec is None:
            return -1
        return timeRemainingSec


    def GetCurrentZOffsetMm(self) -> int:
        state = PrusaLinkClient.Get().GetState()
        if state is None or state.AxisZ is None:
            return -1
        return state.AxisZ #pyright: ignore[reportReturnType]


    def GetCurrentLayerInfo(self) -> Tuple[Optional[int], Optional[int]]:
        return (None, None)


    def ShouldPrintingTimersBeRunning(self) -> bool:
        state = PrusaLinkClient.Get().GetState()
        if state is None:
            return False
        return state.IsPrinting(False)


    def IsPrintWarmingUp(self) -> bool:
        state = PrusaLinkClient.Get().GetState()
        if state is None:
            return False
        return state.IsPrepareOrSlicing()


    def GetTemps(self) -> Tuple[Optional[float], Optional[float]]:
        state = PrusaLinkClient.Get().GetState()
        if state is None:
            return (None, None)
        return (state.HotendActual, state.BedActual)
