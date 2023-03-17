
# Some of the features we need to integrate into the octoeverywhere package only exist on
# some platforms. This is basically an interface that allows us to dynamically control
# if some objects are available depending on the platform.
class Compat:

    _LocalAuthObj = None
    @staticmethod
    def GetLocalAuth():
        return Compat._LocalAuthObj
    @staticmethod
    def SetLocalAuth(obj):
        Compat._LocalAuthObj = obj
    @staticmethod
    def HasLocalAuth():
        return Compat._LocalAuthObj is not None


    _SlipstreamObj = None
    @staticmethod
    def GetSlipstream():
        return Compat._SlipstreamObj
    @staticmethod
    def SetSlipstream(obj):
        Compat._SlipstreamObj = obj
    @staticmethod
    def HasSlipstream():
        return Compat._SlipstreamObj is not None

    # Must implement the smart pause interface.
    #
    # GetAndResetLastPauseNotificationSuppressionTimeSec - Returns None if there is no current suppression or the time of the last time it was requested
    #
    _SmartPauseInterfaceObj = None
    @staticmethod
    def GetSmartPauseInterface():
        return Compat._SmartPauseInterfaceObj
    @staticmethod
    def SetSmartPauseInterface(obj):
        Compat._SmartPauseInterfaceObj = obj
    @staticmethod
    def HasSmartPauseInterface():
        return Compat._SmartPauseInterfaceObj is not None


    _MainsailConfigHandler = None
    @staticmethod
    def GetMainsailConfigHandler():
        return Compat._MainsailConfigHandler
    @staticmethod
    def SetMainsailConfigHandler(obj):
        Compat._MainsailConfigHandler = obj
    @staticmethod
    def HasMainsailConfigHandler():
        return Compat._MainsailConfigHandler is not None
