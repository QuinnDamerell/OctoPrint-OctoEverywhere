from typing import Any, Dict, List, Optional, Tuple

from octoeverywhere.materialsystem import MaterialSystemHelper

from .bambumodels import BambuState


class BambuMaterialSystemBuilder:
    @staticmethod
    def Build(bambuState:BambuState) -> Dict[str, Any]:
        tools = BambuMaterialSystemBuilder._BuildTools(bambuState)
        isMultiTool = len(tools) > 1

        amsRoot = bambuState.ams if isinstance(bambuState.ams, dict) else {}
        rawUnitsValue = amsRoot.get("ams", [])
        rawUnits:List[Dict[str, Any]] = []
        if isinstance(rawUnitsValue, list):
            rawUnits = [unit for unit in rawUnitsValue if isinstance(unit, dict)]

        sources:List[Dict[str, Any]] = []
        units:List[Dict[str, Any]] = []
        routes:List[Dict[str, Any]] = []
        sourceIdByPrintMapping:Dict[int, str] = {}

        trayExistBits = BambuMaterialSystemBuilder._ParseHexBits(amsRoot.get("tray_exist_bits", None))
        trayIsBambuBits = BambuMaterialSystemBuilder._ParseHexBits(amsRoot.get("tray_is_bbl_bits", None))

        # Keep Bambu's numeric AMS ids in physical order. Non-numeric ids are still supported and retain report order.
        orderedUnits:List[Tuple[int, Dict[str, Any]]] = list(enumerate(rawUnits))
        orderedUnits.sort(key=lambda pair: BambuMaterialSystemBuilder._SortNumericFirst(pair[1].get("id", None), pair[0]))
        for unitIndex, (_, rawUnit) in enumerate(orderedUnits[:MaterialSystemHelper.c_MaxUnits]):
            rawUnitId = MaterialSystemHelper.AsStringOrNone(rawUnit.get("id", None))
            if rawUnitId is None:
                rawUnitId = str(unitIndex)
            unitId = f"bambu-ams-{rawUnitId}"
            unit:Dict[str, Any] = {
                "Id": unitId,
                "Index": unitIndex,
                "Name": f"AMS {unitIndex + 1}",
                # `humidity` is a Bambu 1-5 condition level, not a percentage. Newer hardware can additionally report
                # humidity_raw/humidity_percent; only those fields are safe to normalize as a percentage.
                "HumidityPercent": BambuMaterialSystemBuilder._GetPercent(rawUnit, "humidity_raw", "humidity_percent"),
                "TemperatureCelsius": MaterialSystemHelper.AsFloatOrNone(rawUnit.get("temp", None)),
                "PlatformDetails": {
                    "AmsId": rawUnitId,
                    "HumidityLevel": MaterialSystemHelper.AsIntOrNone(rawUnit.get("humidity", None)),
                    "DryTime": MaterialSystemHelper.AsIntOrNone(rawUnit.get("dry_time", None)),
                }
            }
            units.append(MaterialSystemHelper.CleanDict(unit))

            traysValue = rawUnit.get("tray", [])
            trays:List[Dict[str, Any]] = []
            if isinstance(traysValue, list):
                trays = [tray for tray in traysValue if isinstance(tray, dict)]
            numericUnitId = MaterialSystemHelper.AsIntOrNone(rawUnitId)
            for trayListIndex, rawTray in enumerate(trays):
                if len(sources) >= MaterialSystemHelper.c_MaxSources:
                    break
                position = MaterialSystemHelper.AsIntOrNone(rawTray.get("id", None))
                if position is None:
                    position = trayListIndex
                printMappingValue = (numericUnitId * 4 + position) if numericUnitId is not None else len(sources)
                sourceId = f"{unitId}-source-{position}"
                isEmpty:Optional[bool] = None
                if trayExistBits is not None and printMappingValue >= 0:
                    isEmpty = (trayExistBits & (1 << printMappingValue)) == 0
                isBambu:Optional[bool] = None
                if trayIsBambuBits is not None and printMappingValue >= 0:
                    isBambu = (trayIsBambuBits & (1 << printMappingValue)) != 0

                source = BambuMaterialSystemBuilder._BuildSource(
                    sourceId,
                    len(sources),
                    f"AMS {unitIndex + 1} Slot {position + 1}",
                    rawTray,
                    printMappingValue,
                    position,
                    unitId,
                    isEmpty,
                    isBambu
                )
                sources.append(source)
                sourceIdByPrintMapping[printMappingValue] = sourceId

        # H2-series printers report two virtual external slots. Their print-command mapping is nozzle-dependent and is
        # not the classic printer's -1 encoding, so leave PrintMappingValue unknown until that command path supports
        # the H2 routing rules rather than publishing a value that could select the wrong spool.
        rawVirtualSlots = bambuState.vir_slot if isinstance(bambuState.vir_slot, list) else []
        for virtualSlotIndex, rawVirtualSlot in enumerate(rawVirtualSlots):
            if len(sources) >= MaterialSystemHelper.c_MaxSources:
                break
            sourceId = f"bambu-external-spool-{virtualSlotIndex}"
            if virtualSlotIndex == 0:
                name = "Left External Spool"
            elif virtualSlotIndex == 1:
                name = "Right External Spool"
            else:
                name = f"External Spool {virtualSlotIndex + 1}"
            sources.append(BambuMaterialSystemBuilder._BuildSource(
                sourceId,
                len(sources),
                name,
                rawVirtualSlot,
                None,
                virtualSlotIndex,
                None,
                None,
                None
            ))

        # Classic Bambu printers expose one vt_tray external spool. Raw tray id 254 maps to -1 in the existing
        # ams_mapping print command, so keep both values rather than using the command encoding as source identity.
        if len(rawVirtualSlots) == 0 and isinstance(bambuState.vt_tray, dict) and len(sources) < MaterialSystemHelper.c_MaxSources:
            externalSourceId = "bambu-external-spool"
            sources.append(BambuMaterialSystemBuilder._BuildSource(
                externalSourceId,
                len(sources),
                "External Spool",
                bambuState.vt_tray,
                -1,
                0,
                None,
                None,
                None
            ))
            sourceIdByPrintMapping[-1] = externalSourceId

        # Even a Bambu without an AMS has one external source and one tool. Its material is unknown if vt_tray wasn't
        # reported, but the topology remains useful and consistent with the other printer platforms.
        if len(sources) == 0:
            fallbackCount = len(tools) if isMultiTool else 1
            for index in range(fallbackCount):
                sourceId = "bambu-external-spool" if fallbackCount == 1 else f"bambu-external-spool-{index}"
                source:Dict[str, Any] = {
                    "Id": sourceId,
                    "Index": index,
                    "Name": "External Spool" if fallbackCount == 1 else f"Tool {index} External Spool",
                    "Position": index,
                    "PrintMappingValue": -1 if fallbackCount == 1 else None,
                }
                sources.append(MaterialSystemHelper.CleanDict(source))
            if fallbackCount == 1:
                sourceIdByPrintMapping[-1] = "bambu-external-spool"

        currentMapping = BambuMaterialSystemBuilder._TrayIdToPrintMapping(amsRoot.get("tray_now", None))
        targetMapping = BambuMaterialSystemBuilder._TrayIdToPrintMapping(amsRoot.get("tray_tar", None))
        currentSourceId = sourceIdByPrintMapping.get(currentMapping, None) if currentMapping is not None else None
        targetSourceId = sourceIdByPrintMapping.get(targetMapping, None) if targetMapping is not None else None
        # Classic printers have one unambiguous destination tool. H2 routing depends on additional packed fields and
        # optional FTS hardware, so don't manufacture a route until the printer reports enough to identify it safely.
        if currentSourceId is not None and not isMultiTool:
            routes.append({"SourceId": currentSourceId, "ToolId": "tool-0", "State": "loaded"})
        if targetSourceId is not None and targetSourceId != currentSourceId and not isMultiTool:
            routes.append({"SourceId": targetSourceId, "ToolId": "tool-0", "State": "loading"})

        supportsDrying = any(
            any(k in unit for k in ("drying_temp", "drying_state"))
            for unit in rawUnits
        )
        return MaterialSystemHelper.Build(
            {
                "SupportsMultiMaterial": len(sources) > 1,
                "SupportsMultiTool": isMultiTool,
                "SupportsSourceRouting": len(rawUnits) > 0,
                "SupportsDrying": True if supportsDrying else None,
            },
            sources,
            tools,
            units,
            routes,
            {
                "AmsVersion": MaterialSystemHelper.AsIntOrNone(amsRoot.get("version", None)),
                "CurrentTrayId": MaterialSystemHelper.AsIntOrNone(amsRoot.get("tray_now", None)),
                "TargetTrayId": MaterialSystemHelper.AsIntOrNone(amsRoot.get("tray_tar", None)),
                "ExtruderState": MaterialSystemHelper.AsIntOrNone(bambuState.extruder.get("state", None)) if bambuState.extruder is not None else None,
            }
        )

    @staticmethod
    def _BuildTools(bambuState:BambuState) -> List[Dict[str, Any]]:
        rawExtruder = bambuState.extruder if isinstance(bambuState.extruder, dict) else {}
        rawInfoValue = rawExtruder.get("info", [])
        rawInfo:List[Dict[str, Any]] = []
        if isinstance(rawInfoValue, list):
            rawInfo = [item for item in rawInfoValue if isinstance(item, dict)]

        tools:List[Dict[str, Any]] = []
        if len(rawInfo) > 1:
            for listIndex, rawTool in enumerate(rawInfo[:MaterialSystemHelper.c_MaxTools]):
                toolIndex = MaterialSystemHelper.AsIntOrNone(rawTool.get("id", None))
                if toolIndex is None:
                    toolIndex = listIndex
                packedTemp = MaterialSystemHelper.AsIntOrNone(rawTool.get("temp", None))
                actualCelsius:Optional[float] = None
                targetCelsius:Optional[float] = None
                if packedTemp is not None and packedTemp >= 0:
                    actualRaw = packedTemp & 0xFFFF
                    targetRaw = (packedTemp >> 16) & 0xFFFF
                    # Temperatures outside the physical range mean this packed field has a different shape on an
                    # unknown firmware. Omit them rather than surfacing a believable but incorrect measurement.
                    if actualRaw <= 1000:
                        actualCelsius = float(actualRaw)
                    if targetRaw <= 1000:
                        targetCelsius = float(targetRaw)
                if toolIndex == 0:
                    name = "Left Nozzle"
                elif toolIndex == 1:
                    name = "Right Nozzle"
                else:
                    name = f"Nozzle {toolIndex}"
                tools.append(MaterialSystemHelper.BuildTool(
                    toolIndex,
                    name,
                    None,
                    actualCelsius,
                    targetCelsius,
                    platformDetails={
                        "ExtruderId": toolIndex,
                        "Status": MaterialSystemHelper.AsIntOrNone(rawTool.get("stat", None)),
                    }
                ))
            return tools

        return [MaterialSystemHelper.BuildTool(
            0,
            "Nozzle",
            True,
            bambuState.nozzle_temper,
            bambuState.nozzle_target_temper,
            nozzleDiameterMm=bambuState.nozzle_diameter,
            platformDetails={"NozzleType": bambuState.nozzle_type}
        )]

    @staticmethod
    def _BuildSource(
        sourceId:str,
        index:int,
        name:str,
        rawTray:Dict[str, Any],
        printMappingValue:Optional[int],
        position:int,
        unitId:Optional[str],
        isEmpty:Optional[bool],
        isBambu:Optional[bool]
    ) -> Dict[str, Any]:
        hasRfid = isBambu is True or BambuMaterialSystemBuilder._IsNonZeroId(rawTray.get("tag_uid", None)) or BambuMaterialSystemBuilder._IsNonZeroId(rawTray.get("tray_uuid", None))
        materialName = rawTray.get("tray_id_name", None)
        if MaterialSystemHelper.AsStringOrNone(materialName) is None:
            materialName = rawTray.get("tray_sub_brands", None)
        material = MaterialSystemHelper.BuildMaterial(
            rawTray.get("tray_type", None),
            materialName,
            rawTray.get("tray_color", None),
            rawTray.get("remain", None) if hasRfid else None,
            "Bambu Lab" if isBambu is True else None
        )
        source:Dict[str, Any] = {
            "Id": sourceId,
            "Index": index,
            "Name": name,
            "UnitId": unitId,
            "Position": position,
            "PrintMappingValue": printMappingValue,
            "IsEmpty": isEmpty,
            "Material": material,
            "PlatformDetails": {
                "TrayId": MaterialSystemHelper.AsIntOrNone(rawTray.get("id", None)),
                "TrayUuid": MaterialSystemHelper.AsStringOrNone(rawTray.get("tray_uuid", None)),
                "TagUid": MaterialSystemHelper.AsStringOrNone(rawTray.get("tag_uid", None)),
                "TrayInfoIndex": MaterialSystemHelper.AsStringOrNone(rawTray.get("tray_info_idx", None)),
                "CalibrationIndex": MaterialSystemHelper.AsIntOrNone(rawTray.get("cali_idx", None)),
                "K": MaterialSystemHelper.AsFloatOrNone(rawTray.get("k", None)),
                "N": MaterialSystemHelper.AsFloatOrNone(rawTray.get("n", None)),
                "NozzleTempMinCelsius": MaterialSystemHelper.AsFloatOrNone(rawTray.get("nozzle_temp_min", None)),
                "NozzleTempMaxCelsius": MaterialSystemHelper.AsFloatOrNone(rawTray.get("nozzle_temp_max", None)),
            }
        }
        return MaterialSystemHelper.CleanDict(source)

    @staticmethod
    def _ParseHexBits(value:Any) -> Optional[int]:
        text = MaterialSystemHelper.AsStringOrNone(value)
        if text is None:
            return None
        try:
            return int(text, 16)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _SortNumericFirst(value:Any, fallbackIndex:int) -> Any:
        number = MaterialSystemHelper.AsIntOrNone(value)
        if number is not None:
            return (0, number)
        return (1, fallbackIndex)

    @staticmethod
    def _GetPercent(source:Dict[str, Any], *keys:str) -> Optional[float]:
        for key in keys:
            value = MaterialSystemHelper.AsFloatOrNone(source.get(key, None))
            if value is not None and 0.0 <= value <= 100.0:
                return value
        return None

    @staticmethod
    def _IsNonZeroId(value:Any) -> bool:
        text = MaterialSystemHelper.AsStringOrNone(value)
        if text is None:
            return False
        return len(text.replace("0", "")) > 0

    @staticmethod
    def _TrayIdToPrintMapping(value:Any) -> Optional[int]:
        trayId = MaterialSystemHelper.AsIntOrNone(value)
        if trayId is None or trayId == 255:
            return None
        if trayId == 254:
            return -1
        return trayId
