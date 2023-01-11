import threading
import requests

# A helper class for reporting telemetry.
class Telemetry:
    Logger = None
    ServerProtocolAndDomain = "https://octoeverywhere.com"

    @staticmethod
    def Init(logger):
        Telemetry.Logger = logger

    # Sends a telemetry data point to the service. These data points are suggestions, they are filtered and limited
    # by the service, so it may or may not actually accept them.
    #
    # measureStr - required - a string for the name of the measure.
    # valueInt   - required - an int count value for the measure.
    # fieldsOpt  - optional - A dict of fields that's optional. The dict must have string key and <string, int, double, or bool> values.
    # tagsOpt    - optional - A dict of tags that's optional. The dict must have string keys and string values.
    #
    # Example: Telemetry.Write("Test", 1, { "FieldKey":"FieldValue", "FieldKey2":1.5 }, { "TagKey":"TagValue" })
    @staticmethod
    def Write(measureStr, valueInt, fieldsOpt, tagsOpt):
        thread = threading.Thread(target=Telemetry.WriteSync, args=(measureStr, valueInt, fieldsOpt, tagsOpt, ))
        thread.start()

    # Same as Write(), but it blocks on the request. True is returned on success, otherwise False.
    @staticmethod
    def WriteSync(measureStr, valueInt, fieldsOpt, tagsOpt):
        try:
            # Ensure a value is set and ensure it's an int.
            if valueInt is None :
                valueInt = 1
            valueInt = int(valueInt)

            # Build the object to send.
            event = {
                "Name" : measureStr,
                "Value" : valueInt
            }

            if fieldsOpt is not None:
                event["Fields"] = fieldsOpt
            if tagsOpt is not None:
                # Ensure all tags are strings, as is required.
                for key in tagsOpt:
                    tagsOpt[key] = str(tagsOpt[key])
                event["Tags"] = tagsOpt

            # Send the event.
            response = requests.post(Telemetry.ServerProtocolAndDomain+'/api/stats/v2/telemetryaccumulator', json=event, timeout=1*60)

            # Check for success.
            if response.status_code == 200:
                return True

            Telemetry.Logger.warn("Failed to report "+measureStr+", code: "+str(response.status_code))
        except Exception as e:
            Telemetry.Logger.warn("Failed to report "+measureStr+", error: "+str(e))
        return False


    @staticmethod
    def SetServerProtocolAndDomain(protocolAndDomain):
        Telemetry.ServerProtocolAndDomain = protocolAndDomain
