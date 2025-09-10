import os
import logging
import time
import traceback
import threading
from typing import Any, Dict, Optional

import octowebsocket
import requests
import urllib3

import sentry_sdk
from sentry_sdk import Hub
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.threading import ThreadingIntegration

from .exceptions import NoSentryReportException
from .threaddebug import ThreadDebug

# A helper class to handle Sentry logic.
class Sentry:

    # Holds the process logger.
    _Logger:logging.Logger = None #pyright: ignore[reportAssignmentType]

    # Flags to help Sentry get setup.
    IsSentrySetup:bool = False # This is important since the sentry setup is async and is disabled for some plugin modes.
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
    def Setup(versionString:str, distType:str, isDevMode:bool=False, canEnableProfiling:bool=False, filterExceptionsByPackage:bool=False, restartOnCantCreateThreadBug:bool=False):
        # Set the dev mode flag.
        Sentry.IsDevMode = isDevMode
        Sentry.FilterExceptionsByPackage = filterExceptionsByPackage
        Sentry.RestartProcessOnCantCreateThreadBug = restartOnCantCreateThreadBug

        # Only setup sentry if we aren't in dev mode.
        if Sentry.IsDevMode:
            return

        # Spin off a thread to make the sentry config api request and then init the sdk.
        def setupSentryThread():
            # Give it a few attempts, in case the network isn't ready yet.
            attempt = 0
            while attempt < 3:
                try:
                    attempt += 1
                    Sentry._setupSentryInternal(versionString, distType, isDevMode, canEnableProfiling)
                    return
                except Exception as e:
                    if Sentry._Logger is not None:
                        Sentry._Logger.error("Failed to setup sentry, retrying: "+str(e))
                    time.sleep(10)
        thread = threading.Thread(target=setupSentryThread, daemon=True)
        thread.start()


    # Throws on failure.
    @staticmethod
    def _setupSentryInternal(versionString:str, distType:str, isDevMode:bool, canEnableProfiling:bool) -> None:

        # Make the API call to get the Sentry config.
        response = requests.post("https://octoeverywhere.com/api/plugin/sentryconfig", json={'PluginVersion': versionString, 'DistType': distType}, timeout=20)
        if response.status_code != 200:
            raise Exception(f"Failed to get sentry config, status code {response.status_code}")

        # Parse the response.
        responseJson = response.json()
        result = responseJson.get("Result", None)
        if result is None:
            raise Exception(f"Failed to get sentry config, result {result}")

        # First, check if we are enabled at all.
        enabled = result.get("Enabled", None)
        if enabled is None or enabled is False:
            # Sentry is not enabled, just return.
            if Sentry._Logger is not None:
                Sentry._Logger.info("Sentry is disabled by server config.")
            return

        # Get the rates, default to off.
        tracingSampleRate = result.get("TracingSampleRate", None)
        if tracingSampleRate is None or tracingSampleRate > 1.0 or tracingSampleRate < 0.0:
            if Sentry._Logger is not None:
                Sentry._Logger.warning(f"Sentry got an invalid tracing sample rate. {tracingSampleRate}, setting to 0.0")
            tracingSampleRate = 0.0

        profilingSampleRate = result.get("ProfilingSampleRate", None)
        if profilingSampleRate is None or profilingSampleRate > 1.0 or profilingSampleRate < 0.0:
            if Sentry._Logger is not None:
                Sentry._Logger.warning(f"Sentry got an invalid profiling sample rate. {profilingSampleRate}, setting to 0.0")
            profilingSampleRate = 0.0

        errorSampleRate = result.get("ErrorSampleRate", None)
        if errorSampleRate is None or errorSampleRate > 1.0 or errorSampleRate < 0.0:
            if Sentry._Logger is not None:
                Sentry._Logger.warning(f"Sentry got an invalid error sample rate. {errorSampleRate}, setting to 0.0")
            errorSampleRate = 0.0

        # If we can't enable profiling, disable them.
        if canEnableProfiling is False:
            tracingSampleRate = 0.0
            profilingSampleRate = 0.0

        # We don't want sentry to capture error logs, which is it's default.
        # We do want the logging for breadcrumbs, so we will leave it enabled.
        sentryLogging = LoggingIntegration(
            level=logging.INFO,        # Capture info and above as breadcrumbs
            event_level=logging.FATAL  # Only send FATAL errors and above.
        )
        # Setup and init
        sentry_sdk.init(
            dsn= "https://782efc0e5d44c8239aedf79215e3690d@oe-reports.octoeverywhere.com/4",
            integrations= [
                sentryLogging,
                ThreadingIntegration(propagate_hub=True),
            ],
            # This is the recommended format
            release= f"oe-plugin@{versionString}",
            dist= distType,
            environment= "dev" if isDevMode else "production",
            before_send= Sentry._beforeSendFilter,
            sample_rate= errorSampleRate,
            enable_tracing= tracingSampleRate > 0.0,
            traces_sample_rate= tracingSampleRate,
            profiles_sample_rate= profilingSampleRate,
        )

        # Set that sentry is ready to use.
        Sentry.IsSentrySetup = True


    @staticmethod
    def SetPrinterId(printerId:str):
        sentry_sdk.set_context("octoeverywhere", { "printer-id": printerId })


    @staticmethod
    def _beforeSendFilter(event:Any, hint:Any):

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
    def Breadcrumb(msg:str, data:Optional[Dict[Any, Any]]=None, level:str="info", category:str="breadcrumb"):
        if Sentry.IsSentrySetup:
            sentry_sdk.add_breadcrumb(message=msg, data=data, level=level, category=category)


    # Sends an info log to sentry.
    # This is useful for debugging things that shouldn't be happening.
    @staticmethod
    def LogInfo(msg:str, extras:Optional[Dict[Any, Any]]=None) -> None:
        if Sentry._Logger is None:
            return
        Sentry._Logger.info(f"Sentry Info: {msg}")
        if Sentry.IsSentrySetup:
            with sentry_sdk.push_scope() as scope:
                scope.set_level("error")
                if extras is not None:
                    for key, value in extras.items():
                        scope.set_extra(key, value)
                sentry_sdk.capture_message(msg)


    # Sends an error log to sentry.
    # This is useful for debugging things that shouldn't be happening.
    @staticmethod
    def LogError(msg:str, extras:Optional[Dict[Any, Any]]=None) -> None:
        if Sentry._Logger is None:
            return
        Sentry._Logger.error(f"Sentry Error: {msg}")
        if Sentry.IsSentrySetup:
            with sentry_sdk.push_scope() as scope:
                scope.set_level("error")
                if extras is not None:
                    for key, value in extras.items():
                        scope.set_extra(key, value)
                sentry_sdk.capture_message(msg)


    # Logs and reports an exception.
    # If there's no exception, use LogError instead.
    @staticmethod
    def OnException(msg:str, exception:Exception, extras:Optional[Dict[Any, Any]]=None):
        Sentry._handleException(msg, exception, True, extras)


    # Only logs an exception, without reporting.
    @staticmethod
    def OnExceptionNoSend(msg:str, exception:Exception, extras:Optional[Dict[Any, Any]]=None):
        Sentry._handleException(msg, exception, False, extras)


    # Does the work
    @staticmethod
    def _handleException(msg:str, exception:Exception, sendException:bool, extras:Optional[Dict[Any, Any]]=None):

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
        if Sentry.IsSentrySetup and sendException:
            with sentry_sdk.push_scope() as scope:
                scope.set_extra("Exception Message", msg)
                if extras is not None:
                    for key, value in extras.items():
                        scope.set_extra(key, value)
                sentry_sdk.capture_exception(exception)


    # If the exception is that we can't start new thread, this logs it, and then restarts if needed.
    # Returns of the exception was handled.
    _IsHandlingCantCreateThreadException = False
    # pylint: disable=inconsistent-return-statements
    @staticmethod
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
        Sentry.OnException("Can't start new thread - restarting the process.", e)

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


    # A helper for dealing with common websocket / http connection exceptions.
    # We don't want to report these to sentry, as they are common and not actionable.
    @staticmethod
    def IsCommonConnectionException(e:Exception) -> bool:
        try:
            # This means a device was at the IP, but the port isn't open.
            if isinstance(e, ConnectionRefusedError):
                return True
            if isinstance(e, ConnectionResetError):
                return True
            # This means the IP doesn't route to a device.
            if isinstance(e, OSError) and ("No route to host" in str(e) or "Network is unreachable" in str(e)):
                return True
            # This means the other side never responded.
            if isinstance(e, TimeoutError) and "Connection timed out" in str(e):
                return True
            if isinstance(e, octowebsocket.WebSocketTimeoutException):
                return True
            # This just means the server closed the socket,
            #   or the socket connection was lost after a long delay
            #   or there was a DNS name resolve failure.
            if isinstance(e, octowebsocket.WebSocketConnectionClosedException) and ("Connection to remote host was lost." in str(e) or "ping/pong timed out" in str(e) or "Name or service not known" in str(e)):
                return True
            # Invalid host name.
            if isinstance(e, octowebsocket.WebSocketAddressException) and "Name or service not known" in str(e):
                return True
            # We don't care.
            if isinstance(e, octowebsocket.WebSocketConnectionClosedException):
                return True
        except Exception:
            pass
        return False


    # A helper for dealing with common http exceptions, so we don't send them to sentry.
    @staticmethod
    def IsCommonHttpError(e:Exception) -> bool:
        try:
            if isinstance(e, requests.exceptions.ConnectionError):
                return True
            if isinstance(e, requests.exceptions.Timeout):
                return True
            if isinstance(e, requests.exceptions.TooManyRedirects):
                return True
            if isinstance(e, requests.exceptions.URLRequired):
                return True
            if isinstance(e, requests.exceptions.RequestException):
                return True
            if isinstance(e, urllib3.exceptions.ReadTimeoutError):
                return True
        except Exception:
            pass
        return False