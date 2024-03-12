import logging
import time
import traceback

import sentry_sdk
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.threading import ThreadingIntegration
from sentry_sdk import capture_exception

from .exceptions import NoSentryReportException

# A helper class to handle Sentry logic.
class Sentry:

    # Holds the process logger.
    Logger:logging.Logger = None

    # Flags to help Sentry get setup.
    IsSentrySetup:bool = False
    isDevMode:bool = False
    FilterExceptionsByPackage:bool = False
    LastErrorReport:float = time.time()
    LastErrorCount:int = 0


    # This will be called as soon as possible when the process starts to capture the logger, so it's ready for use.
    @staticmethod
    def SetLogger(logger:logging.Logger):
        Sentry.Logger = logger


    # This actually setups sentry.
    # It's only called after the plugin version is known, and thus it might be a little into the process lifetime.
    @staticmethod
    def Setup(versionString:str, distType:str = "unknown", isDevMode:bool = False, enableProfiling:bool = False, filterExceptionsByPackage:bool = False):
        # Set the dev mode flag.
        Sentry.IsDevMode = isDevMode
        Sentry.FilterExceptionsByPackage = filterExceptionsByPackage

        # Only setup sentry if we aren't in dev mode.
        if Sentry.IsDevMode is False:
            try:
                # We don't want sentry to capture error logs, which is it's default.
                # We do want the logging for breadcrumbs, so we will leave it enabled.
                sentry_logging = LoggingIntegration(
                    level=logging.INFO,        # Capture info and above as breadcrumbs
                    event_level=logging.FATAL  # Only send FATAL errors and above.
                )
                # Setup and init
                sentry_sdk.init(
                    dsn="https://5ce4e93a61f09e32634ab4ffc7a865c0@oe-sentry.octoeverywhere.com/6",
                    integrations=[
                        sentry_logging,
                        ThreadingIntegration(propagate_hub=True),
                    ],
                    release=versionString,
                    dist=distType,
                    before_send=Sentry._beforeSendFilter,
                    # This means we will send 100% of errors, maybe we want to reduce this in the future?
                    sample_rate=1.0,
                    # Only enable these if we enable profiling. We can't do it in OctoPrint, because it picks up a lot of OctoPrint functions.
                    traces_sample_rate=0.001 if enableProfiling else 0.0,
                    profiles_sample_rate=0.01 if enableProfiling else 0.0
                )
            except Exception as e:
                if Sentry.Logger is not None:
                    Sentry.Logger.error("Failed to init Sentry: "+str(e))

            # Set that sentry is ready to use.
            Sentry.IsSentrySetup = True


    @staticmethod
    def _beforeSendFilter(event, hint):

        # If we want to filter by package, do it now.
        if Sentry.FilterExceptionsByPackage:
            # Since all OctoPrint plugins run in the same process, sentry will pick-up unhandled exceptions
            # from all kinds of sources. To prevent that from spamming us, if we can pull out a call stack, we will only
            # send things that have some origin in our code. This can be any file in the stack or any module with our name in it.
            # Otherwise, we will ignore it.
            exc_info = hint.get("exc_info")
            if exc_info is None or len(exc_info) < 2 or hasattr(exc_info[2], "tb_frame") is False:
                Sentry.Logger.error("Failed to extract exception stack in sentry before send.")
                return None

            # Check the stack
            shouldSend = False
            try:
                stack = traceback.extract_stack((exc_info[2]).tb_frame)
                for s in stack:
                    # Check for any "octoeverywhere" or "linux_host" in the filename.
                    # This will match one of the main modules in our code, but exclude any 3rd party code.
                    filenameLower = s.filename.lower()
                    if "octoeverywhere" in filenameLower or "linux_host" in filenameLower or "py_installer" in filenameLower:
                        # If found, return the event so it's reported.
                        shouldSend = True
                        break
            except Exception as e:
                Sentry.Logger.error("Failed to extract exception stack in sentry before send. "+str(e))

            # If we shouldn't send, then return None to prevent it.
            if shouldSend is False:
                return None

        # To prevent spamming, don't allow clients to send errors too quickly.
        # We will simply only allows up to 5 errors reported every 4h.
        timeSinceErrorSec = time.time() - Sentry.LastErrorReport
        if timeSinceErrorSec < 60 * 60 * 4:
            if Sentry.LastErrorCount > 5:
                return None
        else:
            # A new time window has been entered.
            Sentry.LastErrorReport = time.time()
            Sentry.LastErrorCount = 0

        # Increment the report counter
        Sentry.LastErrorCount += 1

        # Return the event to be reported.
        return event


    # Logs and reports an exception.
    @staticmethod
    def Exception(msg:str, exception:Exception):
        Sentry._handleException(msg, exception, True)


    # Only logs an exception, without reporting.
    @staticmethod
    def ExceptionNoSend(msg:str, exception:Exception):
        Sentry._handleException(msg, exception, False)


    # Does the work
    @staticmethod
    def _handleException(msg:str, exception:Exception, sendException:bool):

        # This could be called before the class has been inited, in such a case just return.
        if Sentry.Logger is None:
            return

        tb = traceback.format_exc()
        exceptionClassType = "unknown_type"
        if exception is not None:
            exceptionClassType = exception.__class__.__name__
        Sentry.Logger.error(msg + "; "+str(exceptionClassType)+" Exception: " + str(exception) + "; "+str(tb))

        # We have a special exception that we can throw but we won't report it to sentry.
        # See the class for details.
        if isinstance(exception, NoSentryReportException):
            return

        # Never send in dev mode, as Sentry will not be setup.
        if Sentry.IsSentrySetup and sendException and Sentry.IsDevMode is False:
            capture_exception(exception)
