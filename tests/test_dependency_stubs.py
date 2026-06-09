import sys
import types
from typing import Any


def _InstallModule(name:str) -> types.ModuleType:
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        sys.modules[name] = module
    return module


def _InstallSentryStub() -> None:
    sentry = _InstallModule("sentry_sdk")

    class _Hub:
        current = None

    def _NoOp(*args:Any, **kwargs:Any) -> None:
        return None

    sentry.Hub = _Hub
    sentry.init = _NoOp
    sentry.capture_exception = _NoOp
    sentry.capture_message = _NoOp
    sentry.add_breadcrumb = _NoOp
    sentry.set_context = _NoOp
    sentry.set_tag = _NoOp
    sentry.set_user = _NoOp

    integrations = _InstallModule("sentry_sdk.integrations")
    loggingModule = _InstallModule("sentry_sdk.integrations.logging")
    threadingModule = _InstallModule("sentry_sdk.integrations.threading")
    sentry.integrations = integrations
    integrations.logging = loggingModule
    integrations.threading = threadingModule

    class _Integration:
        def __init__(self, *args:Any, **kwargs:Any) -> None:
            pass

    loggingModule.LoggingIntegration = _Integration
    threadingModule.ThreadingIntegration = _Integration


def _InstallOctoWebSocketStub() -> None:
    octowebsocket = _InstallModule("octowebsocket")
    _InstallModule("octowebsocket_client")

    class WebSocketApp:
        def __init__(self, *args:Any, **kwargs:Any) -> None:
            pass

        def run_forever(self, *args:Any, **kwargs:Any) -> None:
            return None

        def close(self, *args:Any, **kwargs:Any) -> None:
            return None

        def send(self, *args:Any, **kwargs:Any) -> None:
            return None

    class WebSocketTimeoutException(Exception):
        pass

    class WebSocketConnectionClosedException(Exception):
        pass

    class WebSocketAddressException(Exception):
        pass

    class WebSocketBadStatusException(Exception):
        pass

    octowebsocket.WebSocketApp = WebSocketApp
    octowebsocket.WebSocketTimeoutException = WebSocketTimeoutException
    octowebsocket.WebSocketConnectionClosedException = WebSocketConnectionClosedException
    octowebsocket.WebSocketAddressException = WebSocketAddressException
    octowebsocket.WebSocketBadStatusException = WebSocketBadStatusException
    octowebsocket.setdefaulttimeout = lambda *args, **kwargs: None


def _InstallOctoPrintStub() -> None:
    octoprint = _InstallModule("octoprint")
    octoprint.__version__ = "test"

    printer = _InstallModule("octoprint.printer")

    class PrinterInterface:
        pass

    printer.PrinterInterface = PrinterInterface
    octoprint.printer = printer


def _InstallDnsStub() -> None:
    dns = _InstallModule("dns")
    resolver = _InstallModule("dns.resolver")

    class LifetimeTimeout(Exception):
        pass

    class Resolver:
        def __init__(self, *args:Any, **kwargs:Any) -> None:
            self.nameservers = []
            self.port = 0

        def resolve(self, *args:Any, **kwargs:Any) -> list:
            return []

    resolver.LifetimeTimeout = LifetimeTimeout
    resolver.Resolver = Resolver
    dns.resolver = resolver


def _InstallFlatbuffersStub() -> None:
    flatbuffers = _InstallModule("octoflatbuffers")

    class _Builder:
        def __init__(self, *args:Any, **kwargs:Any) -> None:
            pass

        def StartObject(self, *args:Any, **kwargs:Any) -> None:
            return None

        def EndObject(self, *args:Any, **kwargs:Any) -> int:
            return 0

        def StartVector(self, *args:Any, **kwargs:Any) -> int:
            return 0

        def PrependUOffsetTRelativeSlot(self, *args:Any, **kwargs:Any) -> None:
            return None

        def PrependInt8Slot(self, *args:Any, **kwargs:Any) -> None:
            return None

    flatbuffers.Builder = _Builder

    packer = types.SimpleNamespace(uoffset=object())
    encode = types.SimpleNamespace(Get=lambda *args, **kwargs: 0)

    class _Flag:
        @staticmethod
        def py_type(value:Any) -> Any:
            return value

    numberTypes = types.SimpleNamespace(UOffsetTFlags=_Flag, Int8Flags=_Flag)

    class _Table:
        def __init__(self, buf:bytes, pos:int) -> None:
            self.Bytes = buf
            self.Pos = pos

        def Offset(self, offset:int) -> int:
            return 0

        def String(self, offset:int) -> None:
            return None

        def Vector(self, offset:int) -> int:
            return 0

        def VectorLen(self, offset:int) -> int:
            return 0

        def Indirect(self, offset:int) -> int:
            return offset

        def Get(self, *args:Any, **kwargs:Any) -> int:
            return 0

    tableModule = _InstallModule("octoflatbuffers.table")
    tableModule.Table = _Table

    flatbuffers.packer = packer
    flatbuffers.encode = encode
    flatbuffers.number_types = numberTypes
    flatbuffers.table = tableModule


def InstallTestDependencyStubs() -> None:
    _InstallSentryStub()
    _InstallOctoWebSocketStub()
    _InstallOctoPrintStub()
    _InstallDnsStub()
    _InstallFlatbuffersStub()
