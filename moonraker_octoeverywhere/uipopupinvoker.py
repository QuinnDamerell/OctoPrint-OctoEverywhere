
# A class to handle the popup notification actions from the service.
class UiPopupInvoker():

    def __init__(self, logger):
        self.Logger = logger


    def ShowUiPopup(self, title, text, msgType, autoHide):
        # Right now, this platform doesn't support notifications, so we will just log them.
        self.Logger.info("Popup Notification Received. Title:"+title+"; Text:"+text+"; Type:"+msgType+"; AutoHide:"+str(autoHide))
