import gc
import io
import logging
import os
import sys
import threading
import tracemalloc
from typing import Any, Dict, Iterable, Optional, Tuple


# A memory debugging utility that periodically logs memory usage and object allocation statistics.
class MemoryDebug:
    def __init__(self, logger: logging.Logger, interval_sec: float = 60.0, top_n: int = 30) -> None:
        self.Logger = logger
        self.IntervalSec = interval_sec
        self.TopN = top_n
        self._stop_event = threading.Event()
        self._objgraph_available: Optional[bool] = None
        self._thread = threading.Thread(target=self._Worker, name="MemoryDebug")
        self._thread.daemon = True
        self._thread.start()
        self.Logger.info("MemoryDebug enabled. Reporting every %.1f seconds.", self.IntervalSec)
        self.Logger.error("NOTE THIS MEMORY TRACKING WILL TAKE UP MORE MEMORY AND SLOW THINGS DOWN!")
        self.Logger.error("IF debugging under VS code, VS code will also hold on to threads when they are dead, which eats memory.")


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
        for idx, (type_name, (count, total_size)) in enumerate(type_summary[: self.TopN], start=1): # pyright: ignore[reportIndexIssue]
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
            # Once you find a class with a lot of instances or that's around for a long time, use this functions to dig deeper.
            #self._InvestigateClass("OctoWebStream")

            # 1. Log the most common types
            most_common = objgraph.most_common_types(limit=self.TopN)
            for idx, (type_name, count) in enumerate(most_common, start=1):
                self.Logger.info("MemoryDebug ObjGraph Top Types %d: type=%s count=%d", idx, type_name, count)

            # 2. Log Growth and Trace the "Leakiest" Object
            if hasattr(objgraph, "growth"):
                growth = objgraph.growth(limit=self.TopN)
                for idx, growth_item in enumerate(growth, start=1):
                    self.Logger.info("MemoryDebug ObjGraph Growth %d: %s", idx, growth_item)

                # If there's growth, trace the first (usually largest) growth item
                if growth:
                    leaky_type = growth[0][0]
                    # Get one instance of this type
                    leaky_objs = objgraph.by_type(leaky_type)
                    if leaky_objs:
                        # Find the chain of references leading to this object
                        # We limit to 10 nodes deep to avoid log spam
                        chain = objgraph.find_backref_chain(leaky_objs[0], objgraph.is_proper_module)
                        self.Logger.info("MemoryDebug Backref Trace for %s: %s", leaky_type, " -> ".join([str(o) for o in chain]))

        except Exception as exc:
            self.Logger.error("MemoryDebug ObjGraph summary failed: %s", str(exc))



    def _InvestigateClass(self, name:str) -> None:
        # 1. Get all OctoWebStream objects currently in memory
        # (Make sure the class name string matches exactly)
        # streams = objgraph.by_type(name) # pyright: ignore[reportUndefinedVariable, reportUnknownMemberType] pylint: disable=E0602, noqa: F821
        # if not streams:
        #     self.Logger.info("No OctoWebStream objects found in memory.")
        # else:
        #     # 2. Pick the first one (usually the oldest leak)
        #     leaked_stream = streams[0]

        #     # 3. Find the chain of references pointing to it
        #     # We use objgraph.is_proper_module to stop the chain at the module level
        #     chain = objgraph.find_backref_chain( # pyright: ignore[reportUnknownMemberType, reportUndefinedVariable] pylint: disable=E0602, noqa: F821
        #         leaked_stream,
        #         objgraph.is_proper_module # pyright: ignore[reportUnknownMemberType, reportUndefinedVariable] pylint: disable=E0602, noqa: F821
        #     )

        #     # 4. Format the chain into a readable string
        #     output = io.StringIO()
        #     output.write(f"Found {len(streams)} OctoWebStream objects.\n")
        #     output.write("Reference Chain for the first object:\n")

        #     for i, obj in enumerate(chain):
        #         obj_type = type(obj).__name__
        #         output.write(f"  [{i}] {obj_type}: {str(obj)[:1000]} ...\n")

        #         # If it's a dict or list, it might be a container holding the object
        #         if isinstance(obj, dict):
        #             # Check if our target is a value or a key?
        #             # (Simplification: just noting it's a dict)
        #             pass

        #     self.Logger.info(output.getvalue())

        #     # Finally, do a deep search for cycles involving this object
        #     self._FindAnyCycle(leaked_stream, "Leaked OctoWebStream")
        pass


    def _FindAnyCycle(self, obj:Any, obj_name="Target", max_depth=5):
        self.Logger.info(f"🔍 Deep searching {obj_name} for cycles (Depth 1-{max_depth})...")
        # Queue: (current_object, path_string, current_depth)
        queue = [(obj, obj_name, 0)]
        visited = {id(obj)}
        while queue:
            current, path, depth = queue.pop(0)
            if depth >= max_depth:
                continue
            referents = gc.get_referents(current)
            for ref in referents:
                # If we found our original object, we found a cycle!
                if ref is obj:
                    self.Logger.info(f"  🚨 CYCLE FOUND (Depth {depth+1}): {path} -> {obj_name}")
                    return
                # If we haven't seen this object yet, add it to the queue
                if id(ref) not in visited:
                    visited.add(id(ref))
                    # Try to name the object for the log
                    type_name = type(ref).__name__
                    new_path = f"{path} -> {type_name}"
                    queue.append((ref, new_path, depth + 1))
        self.Logger.info(f"  ✅ No cycles found within depth {max_depth}.")


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
