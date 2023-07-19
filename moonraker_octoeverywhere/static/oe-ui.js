//
// Logging Helpers
//
var oe_debug_log = false; 
function oe_log(msg)
{
    if(!oe_debug_log)
    {
        return;
    }
    console.log("OE INFO: "+msg)
}
function oe_error(msg)
{
    console.log("OE ERROR: "+msg)
}

oe_do_load = function()
{
    //
    // Popup logic
    //
    var c_oeAutoTimeHideDurationSec = 5.0;
    var eoAutoTimeHideSec = 0.0;
    var oeAutoHideTimerHandle = null;
    var oeActionLinkUrl = null;

    // Create the main pop-up
    var oePopup = document.createElement('div');
    oePopup.className = 'oe-popup';
    oePopup.id = 'oe-popup';
    document.body.appendChild(oePopup);

    // Create the title class
    var popupTitle = document.createElement('div');
    popupTitle.className = "oe-popup-title";
    oePopup.append(popupTitle);

    // Create the body message.
    var popupMsg = document.createElement('div');
    popupMsg.className = "oe-popup-msg";
    oePopup.append(popupMsg);

    // Create the close button container
    var popupCloseButtonContainer = document.createElement('div');
    popupCloseButtonContainer.className = "oe-popup-button-container";
    oePopup.append(popupCloseButtonContainer);

    // Create the close button.
    var popupActionButton = document.createElement('div');
    popupActionButton.classList.add("oe-popup-button");
    popupActionButton.classList.add("oe-popup-button-action");
    popupActionButton.innerHTML = "Learn More"
    popupCloseButtonContainer.append(popupActionButton);

    // Create the close button.
    var popupCloseButton = document.createElement('div');
    popupCloseButton.className = "oe-popup-button";
    popupCloseButton.innerHTML = "Close"
    popupCloseButtonContainer.append(popupCloseButton);

    // Setup the close handler.
    popupCloseButton.addEventListener("click", function(event)
    {
        oe_log("Popup closed clicked.")
        event.preventDefault();
        oe_hide_popup();
    });
    popupActionButton.addEventListener("click", function(event)
    {
        oe_log("Popup action button clicked.")
        event.preventDefault();
        if(oeActionLinkUrl != null)
        {
            window.open(oeActionLinkUrl, "_blank");
        }
        oe_hide_popup();
    });
    oePopup.addEventListener('mouseover', function()
    {
        oe_log("Popup hovered, stopping timer.")
        oe_clear_auto_hide();
    });
    oePopup.addEventListener('mouseleave', function()
    {
        oe_log("Popup mouse leave.")
        oe_setup_auto_hide();
    });
    oePopup.addEventListener('pointerenter', function()
    {
        // For pointer events, the user might touch and not leave again,
        // thus we won't get an exit. In that case, we will just extend the timer,
        // so it still leaves eventually.
        oe_log("Popup pointer entered, extending timer.")
        // Restart the timer and add 5s to whatever time was being used.
        oe_setup_auto_hide(5);
    });

    // Hides the OctoEverywhere pop-up
    function oe_hide_popup()
    {
        oe_log("Hiding popup")
        oePopup.style.opacity = 0;
        oePopup.style.transition = "0.5s";
        oePopup.style.transitionTimingFunction = "ease-in";
        oe_clear_auto_hide();
        setTimeout(function(){
            oePopup.style.visibility = "collapse";
        }, 500);
    }

    // Shows the OctoEverywhere popup
    function oe_show_popup(title, messageHtml, typeStr, actionText = null, actionLink = null, showForSec = c_oeAutoTimeHideDurationSec, onlyShowIfLoadedFromOe = true)
    {
        // First, check if we should be showing this notification.
        if(onlyShowIfLoadedFromOe)
        {
            if(!oe_is_connected_via_oe())
            {
                oe_log("Ignoring notification because it's only for portals loaded via oe. title: "+title)
                return;
            }
        }
        oe_log("Showing popup")

        // Since we have less control in OctoPrint, we normally start your messages with a br.
        // we will remove it here.
        var msgLower = messageHtml.toLowerCase()
        if(msgLower.startsWith("<br>"))
        {
            messageHtml = messageHtml.substring("<br>".length)
        }
        if(msgLower.startsWith("<br/>"))
        {
            messageHtml = messageHtml.substring("<br/>".length)
        }
        if(msgLower.startsWith("<br />"))
        {
            messageHtml = messageHtml.substring("<br />".length)
        }

        // Set the vars in to the UI.
        popupTitle.innerText = title;
        popupMsg.innerHTML = messageHtml;
        switch(typeStr)
        {
            // Don't use yellow, use default for now.
            // case "notice":
            //     popupTitle.style.backgroundColor = "#4b4838"
            //     break;
            case "success":
                popupTitle.style.backgroundColor = "#3d4b38"
                break;
            case "error":
                popupTitle.style.backgroundColor = "#4b3838"
                break;
            default:
            case "notice":
            case "info":
                popupTitle.style.backgroundColor = "#43464F"
                break;
        }

        // Show or hide the action button, if needed.
        if(actionText != null && actionLink != null && typeof actionText === "string" && typeof actionLink === "string" && actionText.length > 0 && actionLink.length > 0)
        {
            popupActionButton.style.display = "block";
            oeActionLinkUrl = actionLink;
            popupActionButton.innerHTML = actionText;
        }
        else
        {
            popupActionButton.style.display = "none";
            oeActionLinkUrl = null;
        }

        oePopup.style.visibility = "visible";
        oePopup.style.opacity = 0;
        oePopup.style.transitionTimingFunction = "ease-out";
        oePopup.style.transition = "0s";
        setTimeout(function(){
            oePopup.style.transition = "0.5s";
            oePopup.style.opacity = 1;
        }, 50);

        // Setup auto hide
        eoAutoTimeHideSec = showForSec;
        oe_setup_auto_hide();
    }

    // If there should be an auto hide timer, this starts it.
    function oe_setup_auto_hide(extraTimeSec = 0)
    {
        oe_clear_auto_hide();
        if(eoAutoTimeHideSec > 0)
        {
            oe_log("Auto hide enabled for "+eoAutoTimeHideSec)
            oeAutoHideTimerHandle = setTimeout(function()
            {
                oe_hide_popup();
            },
            (eoAutoTimeHideSec * 1000) + extraTimeSec);
        }
    }

    // Stops a timeout if there's one running.
    function oe_clear_auto_hide()
    {
        if(oeAutoHideTimerHandle != null)
        {
            oe_log("Auto hide stopped.")
            clearTimeout(oeAutoHideTimerHandle);
            oeAutoHideTimerHandle = null;
        }
    }

    //
    // Websocket Client
    //
    var oe_webSocket = null;
    var oe_connectionAttempt = 0;

    // If known, this is set to the printer id.
    var oe_printerId = null;
    var oe_pluginVersion = null;

    function oe_websocket_connect()
    {
        if(oe_webSocket != null)
        {
            oe_webSocket.close();
        }
        oe_log("Websocket connecting")
        var protocolPrefix = (window.location.protocol === 'https:') ? 'wss:' : 'ws:';
        oe_webSocket = new WebSocket(protocolPrefix + '//' + location.host + "/websocket");
        oe_webSocket.addEventListener("open", (event)=>
        {
            oe_log("Websocket open")
            oe_connectionAttempt = 0;
            // Quick and sloppy, we don't use the full JRPC protocol
            oe_websocket_send(
                {
                    "jsonrpc": "2.0",
                    "method": "server.connection.identify",
                    "params": {
                        "client_name": "OctoEverywhere-BrowserAgent",
                        "version": "1.0.0",
                        "type": "web",
                        "url": "https://octoeverywhere.com"
                    }
                }
            )
            // Also send a DB query to try to get our printer id.
            oe_websocket_send(
                {
                    "jsonrpc": "2.0",
                    "method": "server.database.get_item",
                    "params": {
                        "namespace": "octoeverywhere",
                        "key": "public.printerId",
                    }
                }
            )
            // and plugin version.
            oe_websocket_send(
                {
                    "jsonrpc": "2.0",
                    "method": "server.database.get_item",
                    "params": {
                        "namespace": "octoeverywhere",
                        "key": "public.pluginVersion",
                    }
                }
            )
        });
        oe_webSocket.addEventListener("close", (event) =>
        {
            oe_log("Websocket closed "+event)
            oe_webSocket = null;
            oe_connectionAttempt++;
            if(oe_connectionAttempt > 20)
            {
                oe_connectionAttempt = 20;
            }
            setTimeout(() =>
            {
                oe_websocket_connect();
            }, oe_connectionAttempt * 500);
        });
        oe_webSocket.addEventListener("message", (event)=>
        {
            try
            {
                // We only do a very simple parse, only looking for messages from our agent.
                var msg = JSON.parse(event.data);
                var method = msg["method"]
                if(method !== undefined && method !== null)
                {
                    if(method === "notify_agent_event")
                    {
                        var params = msg["params"]
                        if(params !== undefined && params !== null && params.length > 0)
                        {
                            var data = params[0]
                            var eventName = data["event"]
                            if(eventName !== undefined && eventName === "oe-notification")
                            {
                                oe_log("Got oe notification agent event.")
                                data = data["data"]
                                if(    "title" in data
                                    && "text" in data
                                    && "msg_type" in data
                                    && "show_for_sec" in data
                                    && "only_show_if_loaded_via_oe" in data
                                    )
                                {
                                    // These fields are optional.
                                    var actionText = null;
                                    var actionLink = null;
                                    if("action_text" in data && "action_link" in data)
                                    {
                                        actionText = data["action_text"]
                                        actionLink = data["action_link"]
                                    }
                                    oe_show_popup(data["title"], data["text"], data["msg_type"], actionText, actionLink, data["show_for_sec"], data["only_show_if_loaded_via_oe"])
                                }
                                else
                                {
                                    oe_error("We got a oe-notification but it didn't have the needed fields.")
                                }
                            }
                        }
                    }
                }
                // We also make a db query to get the printer id, which we parse out.
                var result = msg["result"]
                if(result !== undefined)
                {
                    if("namespace" in result && result["namespace"] === "octoeverywhere")
                    {
                        if("key" in result && result["key"] === "public.printerId")
                        {
                            oe_log("Got printer id")
                            oe_printerId = result["value"]
                        }
                        if("key" in result && result["key"] === "public.pluginVersion")
                        {
                            oe_pluginVersion = result["value"]
                            oe_log("Got plugin version: "+oe_pluginVersion)
                        }
                        if(oe_printerId !== null && oe_pluginVersion !== null)
                        {
                            oe_do_notification_check_in(oe_printerId, oe_pluginVersion, oe_is_connected_via_oe());
                        }
                    }
                }
            }
            catch(error)
            {
                console.log("oe ws onmessage error "+error)
            }
        });
    }

    function oe_websocket_send(msg)
    {
        if(oe_webSocket == null)
        {
            console.log("oe websocket can't send - it's not connected.")
            return;
        }
        msg["id"] = Math.floor(Math.random() * 100000);
        oe_webSocket.send(JSON.stringify(msg));
    }

    // Start the first connection
    oe_websocket_connect()

    //
    // Plugin Connection Check and Data Tunneling.
    //
    // This logic determines if the index is being loaded via OctoEverywhere and if so loading
    // the plugin connection page which assists the plugin in terms of the data tunneling.
    //
    //
    function oe_is_connected_via_oe()
    {
        // Start with a to lower case to remove complexity.
        url = window.location.href.toLowerCase();

        // Check if the URL contains our domain name.
        // If so, we know we are loaded via our service.
        return url.indexOf(".octoeverywhere.com") != -1 || url.indexOf(".octoeverywhere.dev") != -1;
    }
    function oe_inject_service_helpers()
    {
        oe_log("Adding service helpers")
        var iframe = document.createElement('iframe');
        iframe.src = "https://octoeverywhere.com/plugin/connectioncheck"
        iframe.setAttribute("style","height:1px;width:1px;");
        iframe.setAttribute("frameBorder","0");
        iframe.setAttribute("scrolling","no");
        document.body.appendChild(iframe);
    }
    function oe_detect_oe_loaded_index_and_inject_helpers()
    {
        // Only if we are connected via OctoEverywhere, inject the service connection helpers.
        if(oe_is_connected_via_oe())
        {
            oe_inject_service_helpers();
        }
    }
    oe_detect_oe_loaded_index_and_inject_helpers();
    //
    // This logic is used to ping the octoeverywhere service when the page is loaded to detect if there are any
    // notifications for this user.
    //
    // This is fired by the websocket on connect, if it can query the printer id.
    function oe_do_notification_check_in(printerId, pluginVersion, isConnectedViaOctoEverywhere)
    {
        // Create the payload
        var payload = {
            "PrinterId": printerId,
            "PluginVersion": pluginVersion,
            "ClientType" : 3, // Matches our server OctoClientTypes
            "IsConnectedViaOctoEverywhere" : isConnectedViaOctoEverywhere
        };

        // Make the JS request to allow the service to be aware of us and connect up.
        oe_log("Starting plugin check")
        fetch("https://octoeverywhere.com/api/plugin/checkin",
        {
            credentials: "omit",
            method: "POST",
            headers:
            {
                "Content-Type": "application/json",
            },
            body: JSON.stringify(payload),
        })
        .then(response => response.json())
        .then(response =>
        {
            try
            {
                if(response.Status !== 200)
                {
                    oe_error("Failed to call api/plugin/checkin; "+response.Status);
                    return;
                }
                oe_log("Starting plugin check success")
                // If there's a notification, fire it.
                if(response.Result.Notification !== undefined && response.Result.Notification !== null)
                {
                    oe_log("Starting plugin check notification")
                    var note = response.Result.Notification;
                    oe_show_popup(note.Title, note.Message, note.Type, note.ActionText, note.ActionLink, note.ShowForSec, note.OnlyShowIfConnectedViaOe);
                }
            }
            catch (error)
            {
                oe_error("Exception in DoNotificationCheckIn "+error)
            }
        })
        .catch((e)=>
        {
            oe_error("failed to make plugin check "+e)
        })
    }
};
// Since we use the async script tag, sometimes we are loaded after the dom is ready, sometimes before.
// If so, do the load work now.
if(document.readyState === 'loading')
{
    oe_log("Deferring load for DOMContentLoaded")
    document.addEventListener('DOMContentLoaded', oe_do_load);
}
else
{
    oe_log("Dom is ready, loading now.")
    oe_do_load()
}