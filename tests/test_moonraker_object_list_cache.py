import logging
import threading
import unittest
from typing import Any, Dict, List, Optional

from tests.test_dependency_stubs import InstallTestDependencyStubs

InstallTestDependencyStubs()

# pylint: disable=wrong-import-position,protected-access
from moonraker_octoeverywhere.moonrakerclient import MoonrakerClient  # noqa: E402


class _FakeJsonRpcResponse:
    def __init__(self, result:Optional[Dict[str, Any]], hasError:bool=False) -> None:
        self._result = result
        self._hasError = hasError

    def HasError(self) -> bool:
        return self._hasError

    def GetResult(self) -> Dict[str, Any]:
        return self._result if self._result is not None else {}

    def GetLoggingErrorStr(self) -> str:
        return "fake error"


def _MakeClient() -> MoonrakerClient:
    # Build the client without running __init__, which is the pattern the other Moonraker client tests use.
    client = MoonrakerClient.__new__(MoonrakerClient)
    client.Logger = logging.getLogger("test")
    client.PrinterObjectListLock = threading.Lock()
    client.PrinterObjectList = None
    client.PrinterObjectListGeneration = 0
    return client


class TestPrinterObjectListCache(unittest.TestCase):
    def test_caches_the_result(self) -> None:
        client = _MakeClient()
        callCount = []

        def _rpc(method:str, *args:Any, **kwargs:Any) -> Any:
            callCount.append(method)
            return _FakeJsonRpcResponse({"objects": ["extruder", "heater_bed"]})
        client.SendJsonRpcRequest = _rpc #pyright: ignore[reportAttributeAccessIssue]

        self.assertEqual(client.GetPrinterObjectList(), ["extruder", "heater_bed"])
        self.assertEqual(client.GetPrinterObjectList(), ["extruder", "heater_bed"])
        # The second call must come from the cache, so the printer is only queried once per connection.
        self.assertEqual(len(callCount), 1)

    # A failed or empty response must not be cached, or we'd be stuck with no object list until reconnect.
    def test_bad_responses_are_not_cached(self) -> None:
        for response in (_FakeJsonRpcResponse(None, hasError=True), _FakeJsonRpcResponse({"objects": []}), _FakeJsonRpcResponse({})):
            client = _MakeClient()
            client.SendJsonRpcRequest = lambda *a, _response=response, **k: _response #pyright: ignore[reportAttributeAccessIssue]
            self.assertIsNone(client.GetPrinterObjectList())
            self.assertIsNone(client.PrinterObjectList)

    def test_clearing_forces_a_requery(self) -> None:
        client = _MakeClient()
        callCount = []

        def _rpc(method:str, *args:Any, **kwargs:Any) -> Any:
            callCount.append(method)
            return _FakeJsonRpcResponse({"objects": ["extruder"]})
        client.SendJsonRpcRequest = _rpc #pyright: ignore[reportAttributeAccessIssue]

        client.GetPrinterObjectList()
        client.ClearPrinterObjectListCache()
        client.GetPrinterObjectList()
        self.assertEqual(len(callCount), 2)

    # The lock must never be held while the query is in flight, since the query blocks on the websocket and
    # would make every other caller wait on a network round trip.
    # This also covers the race it introduces: a cache cleared mid query must not be overwritten by the
    # in flight result, which belongs to the previous connection.
    def test_lock_is_released_during_the_query(self) -> None:
        client = _MakeClient()
        clearFinished:List[bool] = []

        def _rpc(method:str, *args:Any, **kwargs:Any) -> Any:
            # Clear from another thread while the query is "in flight". If the lock were held across the
            # query this would block forever, so join with a timeout and assert rather than hanging the run.
            def _clear() -> None:
                client.ClearPrinterObjectListCache()
                clearFinished.append(True)
            thread = threading.Thread(target=_clear)
            thread.start()
            thread.join(timeout=5)
            return _FakeJsonRpcResponse({"objects": ["extruder"]})
        client.SendJsonRpcRequest = _rpc #pyright: ignore[reportAttributeAccessIssue]

        result = client.GetPrinterObjectList()

        self.assertEqual(clearFinished, [True], "the cache couldn't be cleared during the query, so the lock was held across it")
        # The caller still gets a usable answer.
        self.assertEqual(result, ["extruder"])
        # But it must not be cached, because it's from before the clear.
        self.assertIsNone(client.PrinterObjectList)


if __name__ == "__main__":
    unittest.main()
