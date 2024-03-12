# This is an exception type that can be used to indicate that something bad happened,
# but we don't want to report it to Sentry because there's no logic error.
#
# For example, if we know the moonraker connection details are correct but there's no device to connect to,
# then the server is probably down and we can't do anything about that.
class NoSentryReportException(Exception):

    def __init__(self, message:str = None, exception:Exception = None):
        self.Message = message
        self.Exception = exception
        super().__init__(message)


    def __str__(self) -> str:
        return self._GetMessage()


    def __repr__(self) -> str:
        return self._GetMessage()


    def _GetMessage(self) -> str:
        result = "No Sentry Exception - "
        if self.Message is not None:
            result += f"Message: `{self.Message}`"
        if self.Exception is not None:
            result += f" Exception: {self.Exception}"
        return result
