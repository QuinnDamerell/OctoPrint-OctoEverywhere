from typing import Any, Dict, List, Optional

from octoeverywhere.materialsystem import MaterialSystemHelper


class OctoPrintMaterialSystemBuilder:
    @staticmethod
    def Build(currentTemps:Dict[str, Any]) -> Dict[str, Any]:
        toolNames = [
            key for key, value in currentTemps.items()
            if key.startswith("tool") and key[4:].isdigit() and isinstance(value, dict)
        ]
        toolNames.sort(key=lambda name: int(name[4:]))
        # A connected OctoPrint printer always has tool0, but retain a useful fallback for test doubles and unusual
        # firmware responses that temporarily omit temperature data.
        if len(toolNames) == 0:
            toolNames = ["tool0"]

        tools:List[Dict[str, Any]] = []
        sourceNames:List[str] = []
        for listIndex, toolName in enumerate(toolNames[:MaterialSystemHelper.c_MaxTools]):
            toolNumber = int(toolName[4:])
            rawValue = currentTemps.get(toolName, {})
            raw:Dict[str, Any] = rawValue if isinstance(rawValue, dict) else {}
            # OctoPrint's common API doesn't expose the selected tool. A single tool is unambiguous; on a multi-tool
            # printer IsActive remains absent instead of guessing tool0.
            isActive:Optional[bool] = True if len(toolNames) == 1 else None
            tools.append(MaterialSystemHelper.BuildTool(
                toolNumber,
                toolName,
                isActive,
                raw.get("actual", None),
                raw.get("target", None),
                platformDetails={"TemperatureKey": toolName}
            ))
            sourceNames.append(f"Tool {toolNumber} Filament")
            # BuildOneToOne assigns sources by list order, while tool identity retains OctoPrint's actual number.
            if listIndex + 1 >= MaterialSystemHelper.c_MaxTools:
                break
        return MaterialSystemHelper.BuildOneToOne(tools, sourceNames)
