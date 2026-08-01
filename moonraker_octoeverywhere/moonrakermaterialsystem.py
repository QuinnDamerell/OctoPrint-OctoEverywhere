import re
from typing import Any, Dict, List, Optional

from octoeverywhere.materialsystem import MaterialSystemHelper


class MoonrakerMaterialSystemBuilder:
    c_DefaultExtruderObjectName = "extruder"
    c_U1ExactObjectNames = {"filament_detect", "print_task_config"}
    c_U1ObjectPrefixes = (
        "filament_feed ",
        "filament_entangle_detect ",
        "filament_motion_sensor ",
    )
    c_U1IndexedObjectRegex = re.compile(r"^.+ e(\d+)_filament$")

    @staticmethod
    def GetOptionalQueryObjectNames(printerObjects:Optional[List[str]]) -> List[str]:
        if printerObjects is None:
            return []
        # These objects are from Snapmaker's published U1 Klipper fork. Discover them before querying so the same code
        # remains safe on standard Klipper and other forks. They are only requested for the expanded status response.
        result = [
            name for name in printerObjects
            if name in MoonrakerMaterialSystemBuilder.c_U1ExactObjectNames
            or name.startswith(MoonrakerMaterialSystemBuilder.c_U1ObjectPrefixes)
        ]
        result.sort()
        return result[:MaterialSystemHelper.c_MaxTools + 8]

    @staticmethod
    def Build(
        statusObjectOrEmptyDict:Dict[str, Any],
        extruderObjectNames:List[str],
        activeExtruderName:str
    ) -> Dict[str, Any]:
        tools:List[Dict[str, Any]] = []
        sourceNames:List[str] = []
        for listIndex, objectName in enumerate(extruderObjectNames[:MaterialSystemHelper.c_MaxTools]):
            toolIndex = MoonrakerMaterialSystemBuilder._ExtruderIndex(objectName, listIndex)
            rawValue = statusObjectOrEmptyDict.get(objectName, None)
            raw:Dict[str, Any] = rawValue if isinstance(rawValue, dict) else {}
            platformDetails:Dict[str, Any] = {"ObjectName": objectName}
            # Tool changer extensions are not standardized across Klipper forks. Preserve their useful diagnostic
            # values without interpreting cumulative counters as current health.
            detailKeys = {
                "status": "State",
                "state": "State",
                "real_extruder_stats": "PhysicalState",
                "switch_count": "SwitchCount",
                "error_count": "ErrorCount",
                "retry_count": "RetryCount",
                "last_maintenance_count": "LastMaintenanceCount",
                "extruder_offset": "ExtruderOffset",
            }
            for rawKey, detailKey in detailKeys.items():
                if rawKey in raw:
                    platformDetails[detailKey] = raw[rawKey]

            entangle = MoonrakerMaterialSystemBuilder._GetIndexedObject(
                statusObjectOrEmptyDict, "filament_entangle_detect ", toolIndex
            )
            if entangle is not None:
                platformDetails["EntangleDetectFactor"] = MaterialSystemHelper.AsFloatOrNone(entangle.get("detect_factor", None))

            tools.append(MaterialSystemHelper.BuildTool(
                toolIndex,
                objectName,
                objectName == activeExtruderName,
                raw.get("temperature", None),
                raw.get("target", None),
                nozzleDiameterMm=raw.get("nozzle_diameter", None),
                platformDetails=platformDetails
            ))
            sourceNames.append(f"{objectName} Filament")

        printTaskValue = statusObjectOrEmptyDict.get("print_task_config", None)
        filamentDetectValue = statusObjectOrEmptyDict.get("filament_detect", None)
        isSnapmakerU1 = isinstance(printTaskValue, dict) or isinstance(filamentDetectValue, dict) or any(
            key.startswith("filament_feed ") for key in statusObjectOrEmptyDict
        )
        if not isSnapmakerU1:
            return MaterialSystemHelper.BuildOneToOne(tools, sourceNames)

        return MoonrakerMaterialSystemBuilder._BuildSnapmakerU1(
            statusObjectOrEmptyDict,
            tools,
            printTaskValue if isinstance(printTaskValue, dict) else {},
            filamentDetectValue if isinstance(filamentDetectValue, dict) else {}
        )

    @staticmethod
    def _BuildSnapmakerU1(
        status:Dict[str, Any],
        tools:List[Dict[str, Any]],
        printTask:Dict[str, Any],
        filamentDetect:Dict[str, Any]
    ) -> Dict[str, Any]:
        sources:List[Dict[str, Any]] = []
        routes:List[Dict[str, Any]] = []
        rfidInfo = MoonrakerMaterialSystemBuilder._AsDictList(filamentDetect.get("info", None))

        for listIndex, tool in enumerate(tools):
            toolIndex = MaterialSystemHelper.AsIntOrNone(tool.get("Index", None))
            if toolIndex is None:
                toolIndex = listIndex
            sourceId = f"snapmaker-u1-source-{toolIndex}"
            feed = MoonrakerMaterialSystemBuilder._GetFeedStatus(status, toolIndex)
            motion = MoonrakerMaterialSystemBuilder._GetIndexedObject(status, "filament_motion_sensor ", toolIndex)
            rfid = rfidInfo[toolIndex] if toolIndex < len(rfidInfo) else {}

            exists = MoonrakerMaterialSystemBuilder._GetListBool(printTask, "filament_exist", toolIndex)
            if exists is None and feed is not None and isinstance(feed.get("filament_detected", None), bool):
                exists = bool(feed["filament_detected"])
            materialType = MoonrakerMaterialSystemBuilder._GetListValue(printTask, "filament_type", toolIndex)
            materialName = MoonrakerMaterialSystemBuilder._GetListValue(printTask, "filament_sub_type", toolIndex)
            manufacturer = MoonrakerMaterialSystemBuilder._GetListValue(printTask, "filament_vendor", toolIndex)
            color = MoonrakerMaterialSystemBuilder._GetListValue(printTask, "filament_color_rgba", toolIndex)
            materialType = MoonrakerMaterialSystemBuilder._KnownMaterialValue(materialType)
            materialName = MoonrakerMaterialSystemBuilder._KnownMaterialValue(materialName)
            manufacturer = MoonrakerMaterialSystemBuilder._KnownMaterialValue(manufacturer)
            if materialType is None:
                materialType = MoonrakerMaterialSystemBuilder._KnownMaterialValue(rfid.get("MAIN_TYPE", None))
            if materialName is None:
                materialName = MoonrakerMaterialSystemBuilder._KnownMaterialValue(rfid.get("SUB_TYPE", None))
            if manufacturer is None:
                manufacturer = MoonrakerMaterialSystemBuilder._KnownMaterialValue(rfid.get("VENDOR", None))
            hasRfid = (
                MoonrakerMaterialSystemBuilder._KnownMaterialValue(rfid.get("VENDOR", None)) is not None
                or MoonrakerMaterialSystemBuilder._KnownMaterialValue(rfid.get("MAIN_TYPE", None)) is not None
                or rfid.get("OFFICIAL", None) is True
                or (MaterialSystemHelper.AsIntOrNone(rfid.get("CARD_UID", None)) or 0) != 0
            )
            if MaterialSystemHelper.NormalizeColorHex(color) is None:
                color = MoonrakerMaterialSystemBuilder._RgbIntToHex(rfid.get("RGB_1", None)) if hasRfid else None
            if exists is False and materialType is None and materialName is None and manufacturer is None and not hasRfid:
                color = None

            platformDetails:Dict[str, Any] = {
                "PhysicalExtruderIndex": toolIndex,
                "OfficialFilament": MoonrakerMaterialSystemBuilder._GetListBool(printTask, "filament_official", toolIndex),
                "FilamentEdited": MoonrakerMaterialSystemBuilder._GetListBool(printTask, "filament_edit", toolIndex),
                "FilamentSoft": MoonrakerMaterialSystemBuilder._GetListBool(printTask, "filament_soft", toolIndex),
                "Sku": MoonrakerMaterialSystemBuilder._GetListValue(printTask, "filament_sku", toolIndex),
                "RfidCardUid": rfid.get("CARD_UID", None) if hasRfid else None,
                "RfidOfficial": rfid.get("OFFICIAL", None) if hasRfid else None,
                "RfidManufacturer": rfid.get("MANUFACTURER", None) if hasRfid else None,
                "RfidDiameter": rfid.get("DIAMETER", None) if hasRfid else None,
                "RfidWeight": rfid.get("WEIGHT", None) if hasRfid else None,
                "RfidLength": rfid.get("LENGTH", None) if hasRfid else None,
                "RfidDryingTemp": rfid.get("DRYING_TEMP", None) if hasRfid else None,
                "RfidDryingTime": rfid.get("DRYING_TIME", None) if hasRfid else None,
            }
            if feed is not None:
                platformDetails.update({
                    "FeedModulePresent": feed.get("module_exist", None),
                    "FeedFilamentDetected": feed.get("filament_detected", None),
                    "FeedAutoMode": not feed["disable_auto"] if isinstance(feed.get("disable_auto", None), bool) else None,
                    "FeedState": feed.get("channel_state", None),
                    "FeedError": feed.get("channel_error", None),
                    "FeedErrorState": feed.get("channel_error_state", None),
                    "FeedActionState": feed.get("channel_action_state", None),
                })
            if motion is not None:
                platformDetails.update({
                    "MotionSensorEnabled": motion.get("enabled", None),
                    "MotionSensorFilamentDetected": motion.get("filament_detected", None),
                })

            source = MaterialSystemHelper.CleanDict({
                "Id": sourceId,
                "Index": listIndex,
                "Name": f"Tool {toolIndex} Filament",
                "Position": toolIndex,
                "PrintMappingValue": toolIndex,
                "IsEmpty": not exists if exists is not None else None,
                "Material": MaterialSystemHelper.BuildMaterial(
                    materialType,
                    materialName,
                    color,
                    manufacturer=manufacturer
                ),
                "PlatformDetails": platformDetails,
            })
            sources.append(source)
            routes.append({"SourceId": sourceId, "ToolId": tool["Id"]})

        return MaterialSystemHelper.Build(
            {
                "SupportsMultiMaterial": len(sources) > 1,
                "SupportsMultiTool": len(tools) > 1,
                # A U1's source is physically attached to its tool. Its slicer-to-physical-tool mapping is preserved
                # below, but that is not the same as an AMS source being routable to several tools.
                "SupportsSourceRouting": False,
                "SupportsDrying": False,
            },
            sources,
            tools,
            routes=routes,
            platformDetails={
                "Platform": "SnapmakerU1",
                "LogicalToPhysicalToolMap": MoonrakerMaterialSystemBuilder._CappedSimpleList(printTask.get("extruder_map_table", None), 32),
                "ExtrudersUsed": MoonrakerMaterialSystemBuilder._CappedSimpleList(printTask.get("extruders_used", None), MaterialSystemHelper.c_MaxTools),
                "ReplenishmentToolMap": MoonrakerMaterialSystemBuilder._CappedSimpleList(printTask.get("extruders_replenished", None), MaterialSystemHelper.c_MaxTools),
                "AutoReplenishFilament": printTask.get("auto_replenish_filament", None),
                "ReplenishIgnoreColor": printTask.get("replenish_ignore_color", None),
                "EntangleDetectionEnabled": printTask.get("filament_entangle_detect", None),
                "EntangleDetectionSensitivity": printTask.get("filament_entangle_sen", None),
            }
        )

    @staticmethod
    def _ExtruderIndex(objectName:str, fallback:int) -> int:
        if objectName == MoonrakerMaterialSystemBuilder.c_DefaultExtruderObjectName:
            return 0
        parsed = MaterialSystemHelper.AsIntOrNone(objectName[len("extruder"):])
        return fallback if parsed is None else parsed

    @staticmethod
    def _GetFeedStatus(status:Dict[str, Any], toolIndex:int) -> Optional[Dict[str, Any]]:
        key = f"extruder{toolIndex}"
        for objectName, value in status.items():
            if objectName.startswith("filament_feed ") and isinstance(value, dict):
                feedStatus:Dict[str, Any] = value
                raw = feedStatus.get(key, None)
                if isinstance(raw, dict):
                    return raw
        return None

    @staticmethod
    def _GetIndexedObject(status:Dict[str, Any], prefix:str, toolIndex:int) -> Optional[Dict[str, Any]]:
        for objectName, value in status.items():
            if not objectName.startswith(prefix) or not isinstance(value, dict):
                continue
            match = MoonrakerMaterialSystemBuilder.c_U1IndexedObjectRegex.match(objectName)
            if match is not None and int(match.group(1)) == toolIndex:
                return value
        return None

    @staticmethod
    def _GetListValue(source:Dict[str, Any], key:str, index:int) -> Any:
        value = source.get(key, None)
        if isinstance(value, list) and index < len(value):
            item = value[index]
            if isinstance(item, (str, bool, int, float)):
                return item
        return None

    @staticmethod
    def _GetListBool(source:Dict[str, Any], key:str, index:int) -> Optional[bool]:
        value = MoonrakerMaterialSystemBuilder._GetListValue(source, key, index)
        return value if isinstance(value, bool) else None

    @staticmethod
    def _AsDictList(value:Any) -> List[Dict[str, Any]]:
        if not isinstance(value, list):
            return []
        return [item if isinstance(item, dict) else {} for item in value[:MaterialSystemHelper.c_MaxTools]]

    @staticmethod
    def _CappedSimpleList(value:Any, limit:int) -> Optional[List[Any]]:
        if not isinstance(value, list):
            return None
        return [item for item in value[:limit] if isinstance(item, (str, bool, int, float))]

    @staticmethod
    def _KnownMaterialValue(value:Any) -> Optional[str]:
        text = MaterialSystemHelper.AsStringOrNone(value)
        return None if text is None or text.upper() == "NONE" else text

    @staticmethod
    def _RgbIntToHex(value:Any) -> Optional[str]:
        parsed = MaterialSystemHelper.AsIntOrNone(value)
        if parsed is None or parsed < 0 or parsed > 0xFFFFFF:
            return None
        return f"{parsed:06X}"
