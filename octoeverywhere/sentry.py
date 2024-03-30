import os
import logging
import time
import traceback

import sentry_sdk
from sentry_sdk import Hub
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.threading import ThreadingIntegration

from .exceptions import NoSentryReportException
from .threaddebug import ThreadDebug

# A helper class to handle Sentry logic.
class Sentry:

    # Holds the process logger.
    _Logger:logging.Logger = None

    # Flags to help Sentry get setup.
    IsSentrySetup:bool = False
    IsDevMode:bool = False
    FilterExceptionsByPackage:bool = False
    LastErrorReport:float = time.time()
    LastErrorCount:int = 0
    RestartProcessOnCantCreateThreadBug = False


    # This will be called as soon as possible when the process starts to capture the logger, so it's ready for use.
    @staticmethod
    def SetLogger(logger:logging.Logger):
        Sentry._Logger = logger


    # This actually setups sentry.
    # It's only called after the plugin version is known, and thus it might be a little into the process lifetime.
    @staticmethod
    def Setup(versionString:str, distType:str, isDevMode:bool = False, enableProfiling:bool = False, filterExceptionsByPackage:bool = False, restartOnCantCreateThreadBug:bool = False):
        # Set the dev mode flag.
        Sentry.IsDevMode = isDevMode
        Sentry.FilterExceptionsByPackage = filterExceptionsByPackage
        Sentry.RestartProcessOnCantCreateThreadBug = restartOnCantCreateThreadBug

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
                    dsn= "https://883879bfa2402df86c098f6527f96bfa@oe-sentry.octoeverywhere.com/4",
                    integrations= [
                        sentry_logging,
                        ThreadingIntegration(propagate_hub=True),
                    ],
                    # This is the recommended format
                    release= f"oe-plugin@{versionString}",
                    dist= distType,
                    environment= "dev" if isDevMode else "production",
                    before_send= Sentry._beforeSendFilter,
                    # This means we will send 100% of errors, maybe we want to reduce this in the future?
                    enable_tracing= enableProfiling,
                    sample_rate= 1.0,
                    # Only enable these if we enable profiling. We can't do it in OctoPrint, because it picks up a lot of OctoPrint functions.
                    traces_sample_rate= 0.01 if enableProfiling else 0.0,
                    profiles_sample_rate= 0.01 if enableProfiling else 0.0,
                )
            except Exception as e:
                if Sentry._Logger is not None:
                    Sentry._Logger.error("Failed to init Sentry: "+str(e))

            # Set that sentry is ready to use.
            Sentry.IsSentrySetup = True


    @staticmethod
    def SetPrinterId(printerId:str):
        sentry_sdk.set_context("octoeverywhere", { "printer-id": printerId })


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
                Sentry._Logger.error("Failed to extract exception stack in sentry before send.")
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
                Sentry._Logger.error("Failed to extract exception stack in sentry before send. "+str(e))

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


    # Adds a breadcrumb to the sentry log, which is helpful to figure out what happened before an exception.
    @staticmethod
    def Breadcrumb(msg:str, data:dict = None, level:str = "info", category:str = "breadcrumb"):
        sentry_sdk.add_breadcrumb(message=msg, data=data, level=level, category=category)


    # Sends an error log to sentry.
    # This is useful for debugging things that shouldn't be happening.
    @staticmethod
    def LogError(msg:str, extras:dict = None) -> None:
        if Sentry._Logger is None:
            return
        Sentry._Logger.error(f"Sentry Error: {msg}")
        # Never send in dev mode, as Sentry will not be setup.
        if Sentry.IsSentrySetup and Sentry.IsDevMode is False:
            with sentry_sdk.push_scope() as scope:
                scope.set_level("error")
                if extras is not None:
                    for key, value in extras.items():
                        scope.set_extra(key, value)
                sentry_sdk.capture_message(msg)


    # Logs and reports an exception.
    # If there's no exception, use LogError instead.
    @staticmethod
    def Exception(msg:str, exception:Exception, extras:dict = None):
        Sentry._handleException(msg, exception, True, extras)


    # Only logs an exception, without reporting.
    @staticmethod
    def ExceptionNoSend(msg:str, exception:Exception, extras:dict = None):
        Sentry._handleException(msg, exception, False, extras)


    # Does the work
    @staticmethod
    def _handleException(msg:str, exception:Exception, sendException:bool, extras:dict = None):

        # This could be called before the class has been inited, in such a case just return.
        if Sentry._Logger is None:
            return

        # We have special logic to handle a bug were we can't create new threads due to a deadlock
        # in our websocket lib. This logic will do that, if it returns true, the Exception has handled.
        if Sentry._HandleCantCreateThreadException(Sentry._Logger, exception):
            return

        tb = traceback.format_exc()
        exceptionClassType = "unknown_type"
        if exception is not None:
            exceptionClassType = exception.__class__.__name__
        Sentry._Logger.error(msg + "; "+str(exceptionClassType)+" Exception: " + str(exception) + "; "+str(tb))

        # We have a special exception that we can throw but we won't report it to sentry.
        # See the class for details.
        if isinstance(exception, NoSentryReportException):
            return

        # Never send in dev mode, as Sentry will not be setup.
        if Sentry.IsSentrySetup and sendException and Sentry.IsDevMode is False:
            with sentry_sdk.push_scope() as scope:
                scope.set_extra("Exception Message", msg)
                if extras is not None:
                    for key, value in extras.items():
                        scope.set_extra(key, value)
                sentry_sdk.capture_exception(exception)


    # If the exception is that we can't start new thread, this logs it, and then restarts if needed.
    # Returns of the exception was handled.
    _IsHandlingCantCreateThreadException = False
    @staticmethod
    # pylint: inconsistent-return-statements
    def _HandleCantCreateThreadException(logger:logging.Logger, e:Exception) -> bool:
        # Filter the exception
        if e is not RuntimeError or "can't start new thread" not in str(e):
            return False

        # If we can't restart, return false, and the normal exception handling will occur.
        if Sentry.RestartProcessOnCantCreateThreadBug is False:
            return False

        # If we are already handling this, return False to prevent a loop.
        # We return false so the exception we are reporting will be handled in the normal way.
        if Sentry._IsHandlingCantCreateThreadException:
            return False
        Sentry._IsHandlingCantCreateThreadException = True

        # Log the error
        ThreadDebug.DoThreadDumpLogout(logger, True)
        logger.error("~~~~~~~~~ Process Restarting Due To Threading Bug ~~~~~~~~~~~~")
        Sentry.Exception("Can't start new thread - restarting the process.", e)

        # Flush Sentry
        # Once this is called, Sentry is shutdown, so we must restart.
        try:
            client = Hub.current.client
            if client is not None:
                client.close(timeout=5.0)
        except Exception:
            pass

        # Restart the process - We must use this function to actually force the process to exit
        # The systemd handler will restart us.
        os.abort()
