import gc
import logging
import os
import sys
import threading
import tracemalloc
from typing import Dict, Iterable, Optional, Tuple


class MemoryDebug:
    def __init__(self, logger: logging.Logger, interval_sec: float = 300.0, top_n: int = 10) -> None:
        self.Logger = logger
        self.IntervalSec = interval_sec
        self.TopN = top_n
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._objgraph_available: Optional[bool] = None


    def Start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._Worker, name="MemoryDebug")
        self._thread.daemon = True
        self._thread.start()
        self.Logger.info("MemoryDebug enabled. Reporting every %.1f seconds.", self.IntervalSec)


    def Stop(self) -> None:
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join(timeout=5.0)
        self._thread = None
        self.Logger.info("MemoryDebug disabled.")


    def _Worker(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._LogSnapshot()
            except Exception as exc:
                self.Logger.error("MemoryDebug snapshot failed: %s", str(exc))
            self._stop_event.wait(self.IntervalSec)


    def _LogSnapshot(self) -> None:
        self._EnsureTraceMallocStarted()
        rss_bytes, rss_peak_bytes = self._GetRssBytes()
        traced_current, traced_peak = tracemalloc.get_traced_memory()

        self.Logger.info(
            "MemoryDebug Snapshot: rss=%s rss_peak=%s tracemalloc_current=%s tracemalloc_peak=%s",
            self._FormatBytes(rss_bytes),
            self._FormatBytes(rss_peak_bytes),
            self._FormatBytes(traced_current),
            self._FormatBytes(traced_peak),
        )

        snapshot = tracemalloc.take_snapshot()
        top_stats = snapshot.statistics("lineno")
        for idx, stat in enumerate(top_stats[: self.TopN], start=1):
            frame = stat.traceback[0]
            self.Logger.info(
                "MemoryDebug Top Alloc %d: size=%s count=%d location=%s:%d",
                idx,
                self._FormatBytes(stat.size),
                stat.count,
                frame.filename,
                frame.lineno,
            )

        type_summary = self._GetTypeSummary()
        for idx, (type_name, (count, total_size)) in enumerate(type_summary[: self.TopN], start=1):
            self.Logger.info(
                "MemoryDebug Top Objects %d: type=%s count=%d size=%s",
                idx,
                type_name,
                count,
                self._FormatBytes(total_size),
            )

        self._LogObjGraphSummary()


    def _EnsureTraceMallocStarted(self) -> None:
        if not tracemalloc.is_tracing():
            tracemalloc.start(25)


    def _GetRssBytes(self) -> Tuple[Optional[int], Optional[int]]:
        proc_status = self._ReadProcStatus()
        if proc_status:
            return proc_status.get("VmRSS"), proc_status.get("VmHWM")
        return None, None


    def _ReadProcStatus(self) -> Dict[str, int]:
        status_path = "/proc/self/status"
        if not os.path.exists(status_path):
            return {}
        result: Dict[str, int] = {}
        try:
            with open(status_path, "r", encoding="utf-8") as status_file:
                for line in status_file:
                    if line.startswith("Vm"):
                        parts = line.split()
                        if len(parts) >= 3 and parts[1].isdigit():
                            value_kb = int(parts[1])
                            result[parts[0].rstrip(":")] = value_kb * 1024
        except Exception:
            return {}
        return result


    def _GetTypeSummary(self) -> Iterable[Tuple[str, Tuple[int, int]]]:
        counts: Dict[str, Tuple[int, int]] = {}
        for obj in gc.get_objects():
            try:
                type_name = type(obj).__name__
                size = sys.getsizeof(obj)
            except Exception:
                continue
            count, total_size = counts.get(type_name, (0, 0))
            counts[type_name] = (count + 1, total_size + size)
        return sorted(counts.items(), key=lambda item: item[1][1], reverse=True)


    def _LogObjGraphSummary(self) -> None:
        objgraph = self._TryGetObjGraph()
        if objgraph is None:
            return
        try:
            most_common = objgraph.most_common_types(limit=self.TopN)
            for idx, (type_name, count) in enumerate(most_common, start=1):
                self.Logger.info(
                    "MemoryDebug ObjGraph Top Types %d: type=%s count=%d",
                    idx,
                    type_name,
                    count,
                )
            if hasattr(objgraph, "growth"):
                growth = objgraph.growth(limit=self.TopN)
                for idx, growth_item in enumerate(growth, start=1):
                    self.Logger.info("MemoryDebug ObjGraph Growth %d: %s", idx, growth_item)
        except Exception as exc:
            self.Logger.error("MemoryDebug ObjGraph summary failed: %s", str(exc))


    def _TryGetObjGraph(self):
        if self._objgraph_available is False:
            return None
        try:
            # pylint: disable=import-error
            # pylint: disable=import-outside-toplevel
            import objgraph
            self._objgraph_available = True
            return objgraph
        except Exception:
            if self._objgraph_available is None:
                self.Logger.info("MemoryDebug ObjGraph support unavailable (objgraph not installed).")
            self._objgraph_available = False
        return None


    @staticmethod
    def _FormatBytes(value: Optional[int]) -> str:
        if value is None:
            return "unknown"
        suffixes = ["B", "KB", "MB", "GB", "TB"]
        size = float(value)
        for suffix in suffixes:
            if size < 1024.0:
                return f"{size:.2f}{suffix}"
            size /= 1024.0
        return f"{size:.2f}PB"
