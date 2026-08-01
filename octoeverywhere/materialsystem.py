import math
import re
from typing import Any, Dict, List, Optional, Set


# Helpers for the optional MaterialSystem object returned by the status command.
#
# A material source is where filament is available, while a tool is what extrudes it. Keeping them separate lets the
# same contract describe an AMS (many sources feeding one tool), a tool changer (one source per tool), and hybrid
# printers. Routes are the single source of truth for the relationship between the two.
class MaterialSystemHelper:
    c_MaxSources = 64
    c_MaxTools = 16
    c_MaxUnits = 8
    c_MaxRoutes = 64
    c_MaxStringLength = 512
    c_ColorRegex = re.compile(r"^[0-9A-Fa-f]+$")


    @staticmethod
    def Build(
        capabilities:Dict[str, Optional[bool]],
        sources:List[Dict[str, Any]],
        tools:List[Dict[str, Any]],
        units:Optional[List[Dict[str, Any]]]=None,
        routes:Optional[List[Dict[str, Any]]]=None,
        platformDetails:Optional[Dict[str, Any]]=None
    ) -> Dict[str, Any]:
        # Cap every collection at the shared contract limits before serialization. Printer-supplied topology must not
        # be able to turn the high-frequency status response into an unbounded payload.
        cleanSources = MaterialSystemHelper._UniqueById(sources, MaterialSystemHelper.c_MaxSources)
        cleanTools = MaterialSystemHelper._UniqueById(tools, MaterialSystemHelper.c_MaxTools)
        cleanUnits = MaterialSystemHelper._UniqueById(units or [], MaterialSystemHelper.c_MaxUnits)

        sourceIds = {str(s["Id"]) for s in cleanSources}
        toolIds = {str(t["Id"]) for t in cleanTools}
        cleanRoutes:List[Dict[str, Any]] = []
        for route in routes or []:
            if len(cleanRoutes) >= MaterialSystemHelper.c_MaxRoutes:
                break
            if not isinstance(route, dict):
                continue
            sourceId = route.get("SourceId", None)
            toolId = route.get("ToolId", None)
            if not isinstance(sourceId, str) or not isinstance(toolId, str):
                continue
            if sourceId not in sourceIds or toolId not in toolIds:
                continue
            cleanRoute = MaterialSystemHelper.CleanDict(route)
            if cleanRoute not in cleanRoutes:
                cleanRoutes.append(cleanRoute)

        result:Dict[str, Any] = {
            "Capabilities": MaterialSystemHelper.CleanDict(capabilities),
            "Sources": cleanSources,
            "Tools": cleanTools,
            "Units": cleanUnits,
            "Routes": cleanRoutes,
        }
        cleanPlatformDetails = MaterialSystemHelper.CleanDict(platformDetails or {})
        if len(cleanPlatformDetails) > 0:
            result["PlatformDetails"] = cleanPlatformDetails
        return result


    @staticmethod
    def BuildOneToOne(tools:List[Dict[str, Any]], sourceNames:Optional[List[str]]=None) -> Dict[str, Any]:
        # Platforms that expose tools but no filament metadata still get useful topology and per-tool temperatures.
        # IsEmpty and Material are intentionally omitted because those platforms don't know them.
        sources:List[Dict[str, Any]] = []
        routes:List[Dict[str, Any]] = []
        cleanTools = MaterialSystemHelper._UniqueById(tools, MaterialSystemHelper.c_MaxTools)
        for index, tool in enumerate(cleanTools):
            sourceId = f"source-{index}"
            source:Dict[str, Any] = {
                "Id": sourceId,
                "Index": index,
                "Position": index,
            }
            if sourceNames is not None and index < len(sourceNames):
                source["Name"] = sourceNames[index]
            sources.append(source)
            routes.append({"SourceId": sourceId, "ToolId": tool["Id"]})

        count = len(cleanTools)
        return MaterialSystemHelper.Build(
            {
                "SupportsMultiMaterial": count > 1,
                "SupportsMultiTool": count > 1,
                "SupportsSourceRouting": False,
                "SupportsDrying": False,
            },
            sources,
            cleanTools,
            routes=routes
        )


    @staticmethod
    def BuildTool(
        index:int,
        name:str,
        isActive:Optional[bool],
        actualCelsius:Any,
        targetCelsius:Any,
        isPresent:Optional[bool]=True,
        nozzleDiameterMm:Any=None,
        platformDetails:Optional[Dict[str, Any]]=None
    ) -> Dict[str, Any]:
        tool:Dict[str, Any] = {
            "Id": f"tool-{index}",
            "Index": index,
            "Name": name,
            "IsActive": isActive,
            "IsPresent": isPresent,
            "ActualCelsius": MaterialSystemHelper.AsFloatOrNone(actualCelsius),
            "TargetCelsius": MaterialSystemHelper.AsFloatOrNone(targetCelsius),
            "NozzleDiameterMm": MaterialSystemHelper.AsFloatOrNone(nozzleDiameterMm),
        }
        cleanPlatformDetails = MaterialSystemHelper.CleanDict(platformDetails or {})
        if len(cleanPlatformDetails) > 0:
            tool["PlatformDetails"] = cleanPlatformDetails
        return MaterialSystemHelper.CleanDict(tool)


    @staticmethod
    def BuildMaterial(
        materialType:Any=None,
        name:Any=None,
        colorHex:Any=None,
        remainingPercent:Any=None,
        manufacturer:Any=None
    ) -> Optional[Dict[str, Any]]:
        remaining = MaterialSystemHelper.AsFloatOrNone(remainingPercent)
        # Several platforms use -1 for unknown. Unknown must stay absent rather than looking like an empty spool.
        if remaining is not None and (remaining < 0.0 or remaining > 100.0):
            remaining = None
        result = MaterialSystemHelper.CleanDict({
            "Type": MaterialSystemHelper.AsStringOrNone(materialType),
            "Name": MaterialSystemHelper.AsStringOrNone(name),
            "ColorHex": MaterialSystemHelper.NormalizeColorHex(colorHex),
            "RemainingPercent": remaining,
            "Manufacturer": MaterialSystemHelper.AsStringOrNone(manufacturer),
        })
        return result if len(result) > 0 else None


    @staticmethod
    def CleanDict(value:Dict[str, Any]) -> Dict[str, Any]:
        # Missing data is omitted throughout this contract. In particular, a printer not reporting a boolean must not
        # silently turn into False, and an unknown measurement must not turn into zero.
        result:Dict[str, Any] = {}
        for key, item in value.items():
            if item is None:
                continue
            if isinstance(item, dict):
                nested = MaterialSystemHelper.CleanDict(item)
                if len(nested) > 0:
                    result[str(key)] = nested
            elif isinstance(item, list):
                result[str(key)] = item
            elif isinstance(item, str):
                result[str(key)] = item[:MaterialSystemHelper.c_MaxStringLength]
            elif isinstance(item, float):
                if math.isfinite(item):
                    result[str(key)] = item
            elif isinstance(item, (bool, int)):
                result[str(key)] = item
        return result


    @staticmethod
    def AsStringOrNone(value:Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        if len(text) == 0:
            return None
        return text[:MaterialSystemHelper.c_MaxStringLength]


    @staticmethod
    def AsIntOrNone(value:Any) -> Optional[int]:
        if value is None or isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return None


    @staticmethod
    def AsFloatOrNone(value:Any) -> Optional[float]:
        if value is None or isinstance(value, bool):
            return None
        try:
            parsed = float(value)
            if not math.isfinite(parsed):
                return None
            return round(parsed, 2)
        except (TypeError, ValueError, OverflowError):
            return None


    @staticmethod
    def NormalizeColorHex(value:Any) -> Optional[str]:
        text = MaterialSystemHelper.AsStringOrNone(value)
        if text is None:
            return None
        text = text.lstrip("#")
        # Bambu reports RRGGBBAA. The shared color is RGB because alpha has no meaning for filament.
        if len(text) == 8:
            text = text[:6]
        if len(text) != 6 or MaterialSystemHelper.c_ColorRegex.match(text) is None:
            return None
        return text.upper()


    @staticmethod
    def _UniqueById(items:List[Dict[str, Any]], limit:int) -> List[Dict[str, Any]]:
        result:List[Dict[str, Any]] = []
        ids:Set[str] = set()
        for item in items:
            if len(result) >= limit:
                break
            if not isinstance(item, dict):
                continue
            clean = MaterialSystemHelper.CleanDict(item)
            itemId = clean.get("Id", None)
            if not isinstance(itemId, str) or len(itemId) == 0 or itemId in ids:
                continue
            ids.add(itemId)
            result.append(clean)
        return result
