import logging

import sentry_sdk
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk import capture_exception

# A helper class to handle Sentry logic.
class Sentry:
    logger = None
    isDevMode = False

# Sets up Sentry
    @staticmethod
    def Init(logger, versionString, isDevMode):
        # Capture the logger for future use.
        Sentry.logger = logger

        # Set the dev mode flag.
        Sentry.isDevMode = isDevMode

        # Only setup sentry if we aren't in dev mode.
        if Sentry.isDevMode is False:
            try:
                # We don't want sentry to capture error logs, which is it's default.
                # We do want the logging for breadcrumbs, so we will leave it enabled.
                sentry_logging = LoggingIntegration(
                    level=logging.INFO,        # Capture info and above as breadcrumbs
                    event_level=logging.FATAL  # Only send FATAL errors and above.
                )
                # Setup and init
                sentry_sdk.init(
                    dsn="https://a2eaa1b58ea447f08472545eedfc74fb@o1317704.ingest.sentry.io/6570908",
                    integrations=[
                        sentry_logging,
                    ],
                    traces_sample_rate=1.0,
                    release=versionString,
                )
            except Exception as e:
                logger.error("Failed to init Sentry: "+str(e))


    # Logs and reports an exception.
    @staticmethod
    def Exception(msg, exception):
        Sentry._handleException(msg, exception, True)


    # Only logs an exception, without reporting.
    @staticmethod
    def ExceptionNoSend(msg, exception):
        Sentry._handleException(msg, exception, False)


    # Does the work
    @staticmethod
    def _handleException(msg, exception, sendException):

        # This could be called before the class has been inited, in such a case just return.
        if Sentry.logger is None:
            return

        Sentry.logger.error(msg + "; Exception: " + str(exception))

        # Never send in dev mode, as Sentry will not be setup.
        if sendException and Sentry.isDevMode is False:
            capture_exception(exception)
