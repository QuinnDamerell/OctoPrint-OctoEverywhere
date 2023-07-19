import logging
import threading
import time

from octoeverywhere.sentry import Sentry

from .moonrakerclient import MoonrakerClient, JsonRpcResponse

# A class to handle the popup notification actions from the service.
class UiPopupInvoker():

    def __init__(self, logger:logging.Logger):
        self.Logger = logger
        # pylint: disable=using-constant-test
        if False:
            self.DebugThread = threading.Thread(target=self._NotificationTestWorker)
            self.DebugThread.start()


    # Interface function - Sends a UI popup message for various uses.
    # Must stay in sync with the OctoPrint handler!
    # title - string, the title text.
    # text  - string, the message.
    # type  - string, [notice, info, success, error] the type of message shown.
    # actionText - string, if not None or empty, this is the text to show on the action button or text link.
    # actionLink - string, if not None or empty, this is the URL to show on the action button or text link.
    # onlyShowIfLoadedViaOeBool - bool, if set, the message should only be shown on browsers loading the portal from OE.
    def ShowUiPopup(self, title:str, text:str, msgType:str, actionText:str, actionLink:str, showForSec:int, onlyShowIfLoadedViaOeBool:bool):
        self.Logger.info("Popup Notification Received. Title:"+title+"; Text:"+text+"; Type:"+msgType+"; ShowFor:"+str(showForSec)+" OnlyOe: "+str(onlyShowIfLoadedViaOeBool))

        # Fire an agent custom event, so if any UI is listening it will hear it.
        result = MoonrakerClient.Get().SendJsonRpcRequest("connection.send_event",
        {
            "event": "oe-notification",
            "data": {
                "title": title,
                "text": text,
                "msg_type": msgType,
                "action_text": actionText,
                "action_link": actionLink,
                "show_for_sec": showForSec,
                "only_show_if_loaded_via_oe" : onlyShowIfLoadedViaOeBool,
            }
        })
        # Ignore web socket not connected.
        if result.HasError() and result.ErrorCode != JsonRpcResponse.OE_ERROR_WS_NOT_CONNECTED:
            self.Logger.error("UiPopupInvoker failed to send notification: "+result.GetLoggingErrorStr())


    def _NotificationTestWorker(self):
        self.Logger.warn("Starting notification test worker!")
        while True:
            try:
                time.sleep(20)
                self.ShowUiPopup("🥰 Test Message!", "Message body <strong>with html</strong>. This can also include a lot of things, like <i>italics</i>.", "notice", "And a lot of the time this will have a link!", "https://octoeverywhere.com/supporter?some=arg", 5, False)
            except Exception as e:
                Sentry.Exception("_NotificationTestWorker error", e)
