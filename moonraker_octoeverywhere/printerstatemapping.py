import json
from typing import Any, Dict, List, Optional, Tuple, cast

from .interfaces import IMoonrakerPrinterAdapter
from .printeradapters import MoonrakerPrinterAdapterFactory, SnapmakerU1PrinterAdapter


class PrinterStateMapping:
    ErrorMessageKeys = [
        "message",
        "msg",
        "error_message",
        "error",
        "reason",
        "pause_reason",
        "detail",
        "description",
    ]
    ErrorCodeKeys = [
        "coded",
        "error_code",
        "platform_error_code",
        "code",
    ]

    @staticmethod
    def GetMachineStateManagerSubState(machineStateManager:Optional[Dict[str, Any]],
                                       adapter:Optional[IMoonrakerPrinterAdapter]=None) -> Optional[str]:
        # Preserve the existing helper for callers that explicitly requested the
        # U1 machine-state mapping. New callers should pass their discovered adapter.
        if adapter is None:
            adapter = SnapmakerU1PrinterAdapter()
        return adapter.GetPrinterSubState({"machine_state_manager": machineStateManager})


    @staticmethod
    def GetPrintStatsErrorInfo(printStats:Optional[Dict[str, Any]], supplementalMessage:Optional[str]=None,
                              adapter:Optional[IMoonrakerPrinterAdapter]=None) -> Tuple[Optional[str], Optional[str]]:
        if not isinstance(printStats, dict):
            return (None, None)

        platformErrorCode:Optional[str] = None
        error:Optional[str] = None

        # Some firmware adds a structured exception object to print_stats.
        # Prefer its clean message over print_stats.message, which can contain the
        # raw encoded Klipper exception.
        exceptionObj = printStats.get("exception", None)
        if isinstance(exceptionObj, dict):
            exception = cast(Dict[str, Any], exceptionObj)
            platformErrorCode, error = PrinterStateMapping._GetStructuredErrorInfo(exception, adapter)

        # Several Klipper forks expose a structured object under "error" rather
        # than the standard string-only print_stats.message field.
        errorObj = printStats.get("error", None)
        if isinstance(errorObj, dict):
            structuredCode, structuredError = PrinterStateMapping._GetStructuredErrorInfo(
                cast(Dict[str, Any], errorObj), adapter
            )
            if platformErrorCode is None:
                platformErrorCode = structuredCode
            if error is None:
                error = structuredError

        if platformErrorCode is None:
            codeAdapter = adapter if adapter is not None else MoonrakerPrinterAdapterFactory.GetForError(printStats)
            platformErrorCode = codeAdapter.GetErrorCode(printStats)

        for key in PrinterStateMapping.ErrorMessageKeys:
            fieldCode, fieldError = PrinterStateMapping._GetErrorInfoFromValue(printStats.get(key, None), adapter)
            if platformErrorCode is None:
                platformErrorCode = fieldCode
            if error is None:
                error = fieldError
            if error is not None and platformErrorCode is not None:
                break

        # Some pause macros set an M117/SET_DISPLAY_TEXT message immediately
        # before PAUSE. Only callers with a message from the same status update
        # should provide this fallback, so stale display text isn't reported.
        if error is None:
            error = PrinterStateMapping._CleanNotificationMessage(supplementalMessage)

        # Preserve the existing generic Moonraker behavior when no platform code exists.
        if platformErrorCode is None:
            platformErrorCode = PrinterStateMapping._GetOptionalString(printStats.get("state", None))

        return (platformErrorCode, PrinterStateMapping._CleanNotificationMessage(error))


    @staticmethod
    def GetWebhooksErrorInfo(state:Optional[str], stateMessage:Optional[str], notificationMethod:Optional[str]=None,
                            adapter:Optional[IMoonrakerPrinterAdapter]=None) -> Tuple[Optional[str], Optional[str]]:
        normalizedState = PrinterStateMapping._GetOptionalString(state)
        if normalizedState is not None:
            normalizedState = normalizedState.lower()
        normalizedMethod = PrinterStateMapping._GetOptionalString(notificationMethod)
        if normalizedMethod is not None:
            normalizedMethod = normalizedMethod.lower()

        if normalizedMethod == "notify_klippy_disconnected":
            return ("klippy_disconnected", "Klipper Disconnected")

        if normalizedMethod == "notify_klippy_shutdown":
            code = "klippy_shutdown"
        elif normalizedState is not None and normalizedState.lower() in ["error", "shutdown"]:
            code = "klippy_" + normalizedState.lower()
        else:
            return (None, None)

        error:Optional[str] = None
        if normalizedState is not None and normalizedState.lower() in ["error", "shutdown"]:
            fieldCode, fieldError = PrinterStateMapping._GetErrorInfoFromValue(stateMessage, adapter)
            if fieldCode is not None:
                code = fieldCode
            error = PrinterStateMapping._CleanNotificationMessage(fieldError)
        if error is None:
            isShutdown = normalizedMethod == "notify_klippy_shutdown" or normalizedState == "shutdown"
            error = "Klipper Shutdown" if isShutdown else "Klipper Error"
        return (code, error)


    @staticmethod
    def _GetErrorInfoFromValue(value:Any, adapter:Optional[IMoonrakerPrinterAdapter]=None,
                               depth:int=0) -> Tuple[Optional[str], Optional[str]]:
        # Limit nesting of arbitrary printer data, including recursively encoded JSON.
        if depth > 16:
            return (None, None)
        if isinstance(value, dict):
            return PrinterStateMapping._GetStructuredErrorInfo(cast(Dict[str, Any], value), adapter, depth + 1)

        if isinstance(value, list):
            for item in value:
                code, message = PrinterStateMapping._GetErrorInfoFromValue(item, adapter, depth + 1)
                if code is not None or message is not None:
                    return (code, message)
            return (None, None)

        rawMessage = PrinterStateMapping._GetOptionalString(value)
        if not isinstance(rawMessage, str):
            return (None, None)

        try:
            # Klipper forks can prefix their state_message with a JSON error
            # envelope and append plain text. loads() would reject that suffix.
            encodedMessageObj, end = json.JSONDecoder().raw_decode(rawMessage)
            if isinstance(encodedMessageObj, dict):
                code, message = PrinterStateMapping._GetStructuredErrorInfo(
                    cast(Dict[str, Any], encodedMessageObj), adapter, depth + 1)
            elif isinstance(encodedMessageObj, list):
                code, message = PrinterStateMapping._GetErrorInfoFromValue(encodedMessageObj, adapter, depth + 1)
            else:
                return (None, rawMessage)
            if message is None:
                message = PrinterStateMapping._CleanNotificationMessage(str(rawMessage)[end:])
            # Unknown JSON is still useful diagnostic information. A recognized
            # envelope with no message should let callers use their normal fallback.
            recognized = isinstance(encodedMessageObj, list) or (isinstance(encodedMessageObj, dict) and (
                len(encodedMessageObj) == 0 or any(
                    key in encodedMessageObj for key in PrinterStateMapping.ErrorCodeKeys + PrinterStateMapping.ErrorMessageKeys)))
            if code is not None or message is not None or recognized:
                return (code, message)
        except (TypeError, ValueError, RecursionError):
            pass
        return (None, rawMessage)


    @staticmethod
    def _GetStructuredErrorInfo(errorObj:Dict[str, Any], adapter:Optional[IMoonrakerPrinterAdapter]=None,
                                depth:int=0) -> Tuple[Optional[str], Optional[str]]:
        if depth > 16:
            return (None, None)
        codeAdapter = adapter if adapter is not None else MoonrakerPrinterAdapterFactory.GetForError(errorObj)
        code = codeAdapter.GetErrorCode(errorObj)
        message:Optional[str] = None

        for key in PrinterStateMapping.ErrorMessageKeys:
            nestedCode, nestedMessage = PrinterStateMapping._GetErrorInfoFromValue(errorObj.get(key, None), adapter, depth + 1)
            if code is None:
                code = nestedCode
            if nestedMessage is not None:
                message = nestedMessage
                break

        return (code, message)


    @staticmethod
    def _GetOptionalString(value:Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return None
        result = str(value).strip()
        return result if len(result) > 0 else None


    @staticmethod
    def _CleanNotificationMessage(value:Any) -> Optional[str]:
        message = PrinterStateMapping._GetOptionalString(value)
        if message is None:
            return None

        # Klipper state messages often append recovery instructions or a long
        # traceback. The first paragraph contains the actionable fault.
        stopPrefixes = [
            "Once the underlying issue is corrected",
            "This generally occurs",
            "After correcting the underlying issue",
            "Traceback (most recent call last)",
        ]
        lines:List[str] = []
        for rawLine in message.splitlines():
            line = rawLine.strip()
            if len(line) == 0:
                if len(lines) > 0:
                    break
                continue
            if any(line.startswith(prefix) for prefix in stopPrefixes):
                break
            lines.append(line)

        result = " ".join(lines)
        if len(result) == 0:
            return None
        if len(result) > 1000:
            result = result[:997].rstrip() + "..."
        return result
