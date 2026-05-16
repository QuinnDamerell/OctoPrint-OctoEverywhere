import logging
import threading
from typing import Optional, Tuple

from octoeverywhere.interfaces import IPrinterStateReporter
from octoeverywhere.notificationshandler import NotificationsHandler
from octoeverywhere.util.delayedcallback import DelayedCallback

from .elegoocc2client import ElegooCc2Client
from .elegoocc2filemanager import ElegooCc2FileManager, FileInfo
from .elegoocc2models import PrinterState
from .interfaces import IStateTranslator


class ElegooCc2StateTranslator(IPrinterStateReporter, IStateTranslator):

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
                self.DelayedConnectionLostCallback = DelayedCallback.Create(self.Logger, "ElegooCc2DelayedConnectionLostCallback", self.c_ConnectionLostNotificationDelaySec, self._DelayedConnectionLostCallback)

        self.LastStatus = None
        self.IsWaitingOnPrintInfoToFirePrintStart = False


    def _DelayedConnectionLostCallback(self) -> None:
        self.NotificationsHandler.OnError("Connection to printer lost during a print.")


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
                "Elegoo CC2 printer state change: %s -> %s - Machine Status: %s Sub Status: %s",
                self.LastStatus,
                newStatus,
                pState.MachineStatus,
                pState.SubStatus,
            )
            if self.LastStatus is None:
                pass
            elif pState.IsPrinting(False):
                if self.LastStatus == PrinterState.PRINT_STATUS_PAUSED:
                    self.OnResume(pState)
                else:
                    if PrinterState.IsPrintingState(self.LastStatus, False) is False:
                        self.IsWaitingOnPrintInfoToFirePrintStart = True
                        ElegooCc2FileManager.Get().Sync()
            elif pState.IsPaused():
                self.OnPauseOrTempError(pState)
            elif newStatus == PrinterState.PRINT_STATUS_CANCELLED:
                self.OnCancelled(pState)
            elif newStatus == PrinterState.PRINT_STATUS_COMPLETE:
                self.OnComplete(pState)
            elif newStatus == PrinterState.PRINT_STATUS_ERROR:
                self.OnError(pState)

            self.LastStatus = newStatus

        if self.IsWaitingOnPrintInfoToFirePrintStart:
            printCookie = pState.GetPrintCookie()
            etaSec = pState.GetTimeRemainingSec()
            if printCookie is not None and len(printCookie) > 0 and etaSec is not None and etaSec > 0:
                self.IsWaitingOnPrintInfoToFirePrintStart = False
                if pState.IsPrinting(True):
                    self.OnStart(pState)

        if not isFirstFullSyncResponse and self.NotificationsHandler.IsTrackingPrint():
            if pState.Progress is not None:
                self.OnPrintProgress(pState)


    def OnStart(self, printerState:PrinterState) -> None:
        fileSizeKb = 0
        totalFilamentWeightMg = 0
        fileInfo:Optional[FileInfo] = ElegooCc2FileManager.Get().GetFileInfoFromState(printerState)
        if fileInfo is not None:
            if fileInfo.FileSizeKb is not None:
                fileSizeKb = fileInfo.FileSizeKb
            if fileInfo.EstFilamentWeightMg is not None:
                totalFilamentWeightMg = fileInfo.EstFilamentWeightMg
        self.NotificationsHandler.OnStarted(printerState.GetPrintCookie(), printerState.GetFileNameWithNoExtension(), fileSizeKBytes=fileSizeKb, totalFilamentWeightMg=totalFilamentWeightMg)


    def OnComplete(self, printerState:PrinterState) -> None:
        mostRecentPrint = printerState.GetMostRecentPrintInfo()
        self.NotificationsHandler.OnDone(mostRecentPrint.GetFileNameWithNoExtension(), str(mostRecentPrint.DurationSec))


    def OnPauseOrTempError(self, printerState:PrinterState) -> None:
        mostRecentPrint = printerState.GetMostRecentPrintInfo()
        self.NotificationsHandler.OnPaused(mostRecentPrint.GetFileNameWithNoExtension())


    def OnResume(self, printerState:PrinterState) -> None:
        mostRecentPrint = printerState.GetMostRecentPrintInfo()
        self.NotificationsHandler.OnResume(mostRecentPrint.GetFileNameWithNoExtension())


    def OnCancelled(self, printerState:PrinterState) -> None:
        mostRecentPrint = printerState.GetMostRecentPrintInfo()
        self.NotificationsHandler.OnFailed(mostRecentPrint.GetFileNameWithNoExtension(), None, "cancelled")


    def OnError(self, printerState:PrinterState) -> None:
        self.NotificationsHandler.OnError("Printer error")


    def OnPrintProgress(self, printerState:PrinterState) -> None:
        progress = printerState.Progress
        if progress is None:
            progress = 0.0
        self.NotificationsHandler.OnPrintProgress(None, float(progress))


    def GetPrintTimeRemainingEstimateInSeconds(self) -> int:
        state = ElegooCc2Client.Get().GetState()
        if state is None:
            return -1
        timeRemainingSec = state.GetMostRecentPrintInfo().GetTimeRemainingSec()
        if timeRemainingSec is None:
            timeRemainingSec = state.GetTimeRemainingSec()
        if timeRemainingSec is None:
            return -1
        return timeRemainingSec


    def GetCurrentZOffsetMm(self) -> int:
        (currentLayer, _) = self.GetCurrentLayerInfo()
        if currentLayer is None:
            return -1
        return currentLayer * 20


    def GetCurrentLayerInfo(self) -> Tuple[Optional[int], Optional[int]]:
        state = ElegooCc2Client.Get().GetState()
        if state is None:
            return (0, 0)
        if state.IsPrepareOrSlicing():
            return (0, 0)
        currentLayer = state.GetMostRecentPrintInfo().CurrentLayer
        totalLayers = state.GetMostRecentPrintInfo().TotalLayer
        if currentLayer is None:
            currentLayer = 0
        if totalLayers is None:
            totalLayers = 0
        return (currentLayer, totalLayers)


    def ShouldPrintingTimersBeRunning(self) -> bool:
        state = ElegooCc2Client.Get().GetState()
        if state is None:
            return False
        return state.IsPrinting(False)


    def IsPrintWarmingUp(self) -> bool:
        state = ElegooCc2Client.Get().GetState()
        if state is None:
            return False
        if not self.ShouldPrintingTimersBeRunning():
            return False
        return state.IsPrepareOrSlicing()


    def GetTemps(self) -> Tuple[Optional[float], Optional[float]]:
        state = ElegooCc2Client.Get().GetState()
        if state is None:
            return (None, None)
        return (state.HotendActual, state.BedActual)
