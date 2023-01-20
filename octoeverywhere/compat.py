
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


    _SmartPauseObj = None
    @staticmethod
    def GetSmartPause():
        return Compat._SmartPauseObj
    @staticmethod
    def SetSmartPause(obj):
        Compat._SmartPauseObj = obj
    @staticmethod
    def HasSmartPause():
        return Compat._SmartPauseObj is not None
