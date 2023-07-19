/*
 * View model for octoeverywhere
 *
 * Author: Quinn Damerell
 * License: AGPLv3
 */
$(function() {
    function OctoeverywhereViewModel(parameters)
    {
        //
        // Common Stuff
        //
        var self = this;

        // Used for the settings page to get the URL
        self.printerURL = ko.observable()

        function Log(text)
        {
            console.log("OctoEverywhere: "+text)
        }

        function LogError(text)
        {
            console.error("OctoEverywhere: "+text)
        }

        function IsConnectedViaOctoEverywhere()
        {
            // Start with a to lower case to remove complexity.
            url = window.location.href.toLowerCase();

            // Check if the URL contains our domain name.
            // If so, we know we are loaded via our service.
            return url.indexOf(".octoeverywhere.com") != -1 || url.indexOf(".octoeverywhere.dev") != -1;
        }

        function FormatNotificationMsg(text, actionText, actionLink)
        {
            // Wrap the message in a div, so we can add some spacing.
            var msg = `<div style="margin-top:12px;">`+text+"</div>";

            // If there's an action text and link, add the button.
            if(actionText !== undefined && actionLink !== undefined && actionText != null && actionLink != null && typeof actionText === "string" && typeof actionLink === "string" && actionText.length > 0 && actionLink.length > 0)
            {
                // We use this octoprint button style for the action button.
                // We force the button to be 100% width. We have to warp it in a flex div, otherwise the 100% width extends out of the right of the notification.
                msg += `<div style="display:flex; margin-top:12px;"><a class="btn btn-primary" style="color:white;width:100%" target="_blank" href="${actionLink}">${actionText}</a></div>`
            }
            return msg;
        }

        // Used by the wizard to get the printer id.
        self.onWizardDetails = function (response) {
            if (response.octoeverywhere.details.AddPrinterUrl){
                self.printerURL(response.octoeverywhere.details.AddPrinterUrl)
            }
        };

        // Used by the py code to popup UI messages for various things.
        self.onDataUpdaterPluginMessage = function (plugin, data) {
            // check if it's for us.
            if (plugin !== "octoeverywhere_ui_popup_msg") {
                return
            }

            // Format the message the way we do for OctoPrint.
            var msg = FormatNotificationMsg(data.text, data.actionText, data.actionLink)

            // Show a notification.
            var showForMs = (data.showForSec * 1000)
            new PNotify({
                'title': data.title,
                'text':  msg,
                'type':  data.type,
                'hide':  showForMs > 0 ? true : false,
                'delay': showForMs,
                'mouse_reset' : true,
                'icon' : false /* disable since we will use our own */
            });
        }

        // ;)
        console.log("***********************************")
        console.log("   Hello From OctoEverywhere! 🚀   ")
        console.log("***********************************")

        //
        // Index Session Validation
        //
        // What's this?
        //
        // Normally for OctoPrint the client's first call to the index will check the client's session to ensure they are logged in
        // and have permission to read the settings, which is required to load the main index. If they don't, the index call is redirected to the login page.
        // However, since we cache the index page to speed things up, that logic won't happen.
        // In our case, this index page will always be sent back. So we need to use the following logic to ensure the user is logged in and has permissions
        // to read the settings, so the page load doesn't fail.
        //
        // A few notes. The page start up is as follows:
        //    Start the DataUploaded (which is the WS connection)
        //    Call passive login to get the user
        //    Call to get the settings.
        //
        // Normally we would be able to use the loginStateViewModel to access the current user and get callbacks when permissions change.
        // However, during the startup process all of the returned user info is hidden in the class and deferred to be processed until after the settings are read.
        // That means for us we can't get access to that object until the settings fail to load and it's too late.
        // To work around that, we will make our own passive login call which does two things:
        //    1) It ensures the cookie set in the browser is refreshed and not stale (if we made an normal API call we race the internal passive login call and can have a stale cookie.)
        //    2) It returns us the exact permissions the user has, and thus we can tell if they are logged in and/or can access the settings.
        //
        // Important! When OctoPrint updated from 1.8.2 -> 1.8.3 there was an issue. For some reason the passive login check failed which caused an error, but the default wasn't to hit the login
        // page. That caused the index to fail to load, and got the user stuck.
        // THUS IT'S IMPORTANT TO ASSUME ALL ERRORS MEAN LOGIN IS NEEDED!
        // This might catch cases where login isn't needed, but that's better than not catching cases where we need to login but don't.
        //
        // Only do this is being used via OctoEverywhere, since that's the only time this index cache will be a problem.
        self.doLoginRedirect = function()
        {
            Log("Unauthed session detected. Redirecting to login.");
            window.location.href = "/login/?isFromOe=true"
        };
        if(IsConnectedViaOctoEverywhere())
        {
            try
            {
                // Using the OctoPrint JS lib (which is already loaded into this page)
                // make the passive login call.
                OctoPrint.browser
                    .passiveLogin()
                    .done(function(result)
                    {
                        // Validate
                        if(result === null || result.needs === undefined || result.needs.group === undefined)
                        {
                            Log("Returned passive login user doesn't have expected properties.");
                            // On all errors redirect to login to be safe. (see comment in title block)
                            self.doLoginRedirect();
                            return;
                        }

                        // Ideally we use the roles array, but for no logged in users that are "guests", it doesn't exist.
                        // If the user is not logged in, they will be treated as a "guests" in the user group.
                        if(result.needs.role === undefined)
                        {
                            for (let i = 0; i < result.needs.group.length; ++i)
                            {
                                if(result.needs.group[i].toLowerCase() == "guests")
                                {
                                    // We know the guest group doesn't have permission to settings, and thus the user needs to log in.
                                    // The guest group is a generated anonymous user that gets assigned to any session that's no logged in.
                                    self.doLoginRedirect();
                                    return;
                                }
                            }

                            // This shouldn't happen.
                            Log("Returned passive doesn't have guests group role but also doesn't have a role array. "+result.needs.group);
                            // On all errors redirect to login to be safe. (see comment in title block)
                            self.doLoginRedirect();
                            return;
                        }

                        // Use the role array to check if the logged in user has the correct permission.
                        var settingsRoleFound = false;
                        for (let i = 0; i < result.needs.role.length; ++i)
                        {
                            // If the user has either the settings (read and write) or settings_read (read only) they are good.
                            const role = result.needs.role[i].toLowerCase();
                            if(role === "settings" || role === "settings_read")
                            {
                                settingsRoleFound = true;
                                break;
                            }
                        }

                        // If the settings permission wasn't found, redirect to login.
                        if(!settingsRoleFound)
                        {
                            self.doLoginRedirect();
                            return;
                        }
                    })
                    .fail(function ()
                    {
                        // This fail will only occur if something is very wrong, like the network can't be reached.
                        // If no user is logged in, done() will still be called with an anonymous user.
                        LogError("Passive login operation failed.");
                        // On all errors redirect to login to be safe. (see comment in title block)
                        self.doLoginRedirect();
                    });
                }
            catch(error)
            {
                LogError("Failed to make passive login call." + error);
                // On all errors redirect to login to be safe. (see comment in title block)
                self.doLoginRedirect();
            }
        }

        //
        // Local Frontend Port Detection
        //
        //
        // What's this?
        //
        // We have an interesting problem where by default most all users run the OctoPrint http proxy
        // on port 80, but some don't. In our relay logic, we talk directly to OctoPrint PY server via it's port
        // and we can query that to know it for 100% sure. However, webcams can be setup in many ways. We already cover
        // the absolute local URL case with out logic, but we can't cover relative URLs with that logic.
        //
        // So, for all of the reasons above, we need to reliably know what port the http proxy is running on - so if we
        // need to make relative url request, we know the correct port. To get that port reliably, we will wait until the user
        // to use the portal locally as they normally would. When we see that local request, we capture the port and send it to
        // our backend.
        function ReportLocalFrontendPort(port, isHttps, fullUrl)
        {
            Log("Local frontend port found [port:"+port+" isHttps:"+isHttps+" url:"+fullUrl+"] reporting to backend.")
            const xhr = new XMLHttpRequest();
            xhr.onload = () => {
                if (xhr.status > 299) {
                    LogError("Failed to report frontend port to OctoEverywhere API. " + port)
                }
            };
            const payload = {
                "command":"setFrontendLocalPort",
                "port": port,
                "isHttps": isHttps,
                "url": fullUrl
            };
            xhr.open('POST', '/api/plugin/octoeverywhere');
            xhr.setRequestHeader('Content-Type', 'application/json');
            xhr.send(JSON.stringify(payload));
        }

        function DetermineHostnameIsLocalAndReport(hostname, port, isHttps, fullUrl)
        {
            // Now, we have to figure out if this is a local address or not.
            //
            // This logic isn't prefect, but we will consider any address that either an IP or .local a local IP address.
            // But this will false positive is a users access their computer publicly directly via a IP or something.

            // Detect IPV6
            // IPV6 must be enclosed in []
            if(hostname.indexOf("[") != -1 && hostname.indexOf("]") != -1)
            {
                Log("Current hostname detected as IPV6. "+hostname);
                ReportLocalFrontendPort(port, isHttps, fullUrl);
                return;
            }

            // Detect .local addresses
            // Check for the domain name suffix
            if(hostname.endsWith(".local"))
            {
                Log("Current hostname detected as a .local domain. "+hostname);
                ReportLocalFrontendPort(port, isHttps, fullUrl);
                return;
            }

            // Detect IPV4
            // Check if the entire hostname is only numbers and '.'
            var isIPv4 = true;
            for(var i = 0; i < hostname.length; i++)
            {
                var c = hostname[i]
                if(c == '.' || !isNaN(parseInt(c)))
                {
                    continue;
                }
                isIPv4 = false;
                break;
            }
            if(isIPv4)
            {
                Log("Current hostname detected as a IPv4. "+hostname);
                ReportLocalFrontendPort(port, isHttps, fullUrl);
                return;
            }

            // We don't think this address is local.
            Log("Current hostname isn't detected as a local URL "+hostname)
        }

        function FindAndReportLocalFrontendPort(url)
        {
            // Start with a to lower case to remove complexity.
            var url = url.toLowerCase();

            // Look for the protocol end
            const protocolEndStr = "://"
            var protocolEnd = url.indexOf(protocolEndStr)
            if(protocolEnd == -1)
            {
                LogError("No protocol could be found in url "+ url)
                return;
            }

            // Move past the ://
            protocolEnd += protocolEndStr.length

            // Find the end of the hostname and optionally port.
            var hostnameEnd = url.indexOf("/", protocolEnd);
            if(hostnameEnd == -1)
            {
                // If there is no / use the full URL length.
                hostnameEnd = url.length;
            }

            // Validate
            if(hostnameEnd <= protocolEnd)
            {
                LogError("Hostname parse failed. hostnameEnd "+ hostnameEnd + " protocolEnd "+protocolEnd+" url "+ url);
                return;
            }

            // Get the hostname
            var hostname = url.substring(protocolEnd, hostnameEnd)

            // Determine if the protocol is http or https.
            var isHttps = url.startsWith("https://")

            // IPV6 address will be in the following format
            // http://[add:res:tes:blah]:port/stuff
            // Since the following logic is trying to find the port delimiter ':' we need to make sure
            // it doesn't find any of the ':' in the []
            var portDelimiterSearchStart = 0;
            if(hostname.indexOf('[') != -1 && hostname.indexOf(']') != -1)
            {
                // This looks like an IPV6 address, so set the starting point to be at the last ']'
                // to make sure we actually find the port.
                portDelimiterSearchStart = hostname.indexOf(']')
            }

            // Unless we see a ":", we know the port must be 80 or 443
            var hasPortDelimiter = hostname.indexOf(":", portDelimiterSearchStart)
            if(hasPortDelimiter == -1)
            {
                // Check the url for https or not.
                if(isHttps)
                {
                    DetermineHostnameIsLocalAndReport(hostname, 443, isHttps, url);
                }
                else
                {
                    DetermineHostnameIsLocalAndReport(hostname, 80, isHttps, url);
                }
                return;
            }

            // We have a port, parse it out.
            var port = parseInt(hostname.substring(hasPortDelimiter + 1))
            if(port == NaN)
            {
                LogError("Failed to parse port from hostname. "+hostname)
                return;
            }

            // And parse out the main hostname
            hostname = hostname.substring(0, hasPortDelimiter)
            DetermineHostnameIsLocalAndReport(hostname, port, isHttps, url);
        }
        FindAndReportLocalFrontendPort(window.location.href);

        // For testing.
        // FindAndReportLocalFrontendPort("https://octoeverywhere.com");
        // FindAndReportLocalFrontendPort("http://test.local.octoeverywhere.com");
        // FindAndReportLocalFrontendPort("http://test.local.octoeverywhere.com:255");
        // FindAndReportLocalFrontendPort("http://192.168.1.2:255");
        // FindAndReportLocalFrontendPort("http://192.168.1.2/hello");
        // FindAndReportLocalFrontendPort("https://192.168.1.2");
        // FindAndReportLocalFrontendPort("http://octoprint.local");
        // FindAndReportLocalFrontendPort("http://octoprint.local:555");
        // FindAndReportLocalFrontendPort("http://octoprint.local/test");
        // FindAndReportLocalFrontendPort("hTTps://octoprint.local/test");
        // FindAndReportLocalFrontendPort("httpS://octoprint.local:555/test");
        // FindAndReportLocalFrontendPort("https://[2001:0db8:85a3:0000:0000:8a2e:0370:7334]/test");
        // FindAndReportLocalFrontendPort("https://[2001:0db8:85a3:0000:0000:8a2e:0370:7334]:555/test");
        // FindAndReportLocalFrontendPort("http://[2001:0db8:85a3:0000:0000:8a2e:0370:7334]:78945");

        //
        // Plugin Connection Check and Data Tunneling.
        //
        // This logic determines if the index is being loaded via OctoEverywhere and if so loading
        // the plugin connection page which assists the plugin in terms of the data tunneling.
        //
        //
        function InjectServiceHelpers()
        {
            var iframe = document.createElement('iframe');
            iframe.src = "https://octoeverywhere.com/plugin/connectioncheck"
            iframe.setAttribute("style","height:1px;width:1px;");
            iframe.setAttribute("frameBorder","0");
            iframe.setAttribute("scrolling","no");
            document.body.appendChild(iframe);
        }
        function DetectOctoEverywhereLoadedIndexAndInjectionHelpers()
        {
            // Only if we are connected via OctoEverywhere, inject the service connection helpers.
            if(IsConnectedViaOctoEverywhere())
            {
                Log("OctoEverywhere based loading detected.");
                InjectServiceHelpers();
            }
        }
        DetectOctoEverywhereLoadedIndexAndInjectionHelpers();

        //
        //
        //
        //
        // This logic is used to ping the octoeverywhere service when the page is loaded to detect if there are any
        // notifications for this user.
        //
        function DoNotificationCheckIn(printerId, pluginVersion, isConnectedViaOctoEverywhere)
        {
            // Create the payload
            var payload = {
                "PrinterId": printerId,
                "PluginVersion": pluginVersion,
                "ClientType" : 0, // Matches our server OctoClientTypes
                "IsConnectedViaOctoEverywhere" : isConnectedViaOctoEverywhere
            };

            // Make the JS request to allow the service to be aware of us and connect up.
            $.ajax({
                url: "https://octoeverywhere.com/api/plugin/checkin",
                type: "POST",
                dataType: "json",
                data: JSON.stringify(payload),
                contentType: "application/json; charset=UTF-8",
                success: function(response) {
                    try
                    {
                        if(response.Status !== 200)
                        {
                            LogError("Failed to call api/plugin/checkin; "+response.Status);
                            return;
                        }
                        // If there's a notification, fire it.
                        if(response.Result.Notification !== undefined && response.Result.Notification !== null)
                        {
                            var note = response.Result.Notification;

                            // Format the message the way we do for OctoPrint.
                            var msg = FormatNotificationMsg(note.Message, note.ActionText, note.ActionLink);
                            var showForMs = (note.ShowForSec * 1000);

                            // Show a notification.
                            new PNotify({
                                'title': note.Title,
                                'text':  msg,
                                'type':  note.Type,
                                'hide':  showForMs > 0 ? true : false,
                                'delay': showForMs,
                                'mouse_reset' : true,
                                'icon' : false /* disable since we will use our own */
                            });
                        }
                        // If the printer name is returned and this session is connected via OctoEverywhere, update the title so it's easier for users to tell multiple printers apart.
                        if(response.Result.PrinterName !== undefined && response.Result.PrinterName !== null)
                        {
                            if(IsConnectedViaOctoEverywhere())
                            {
                                document.title = document.title + " - " + response.Result.PrinterName
                            }
                        }
                        // Setup the settings UI to reflect if this printer is setup or not.
                        if(response.Result.IsLinked !== undefined)
                        {
                            // If the printer is linked, don't show the default setup help.
                            if(response.Result.IsLinked === true)
                            {
                                // Setup a worker to update the UI. Sometimes if this API call comes back really fast,
                                // it can seem to execute before the UI is fully loaded and these elements don't exist.
                                // In that case we will wait for them to exist and the set them.
                                var worker = function()
                                {
                                    var notSetupUi = $("#octoeverywhere-printer-not-setup");
                                    var setupUi = $("#octoeverywhere-setup-complete");
                                    if(notSetupUi.length === 0 || setupUi.length === 0)
                                    {
                                        console.log("OE is waiting for the settings to load.")
                                        setTimeout(worker, 1000);
                                        return;
                                    }
                                    notSetupUi.hide();
                                    setupUi.show();
                                }
                                worker();
                            }
                        }
                    }
                    catch (error)
                    {
                        LogError("Exception in DoNotificationCheckIn; "+error)
                    }
                },
                failed: function(error){
                    LogError("Failed to call plugin check in API "+error);
                }
            });
        }

        // Called when our plugin settings are ready and can be used.
        function OnSettingsReady(octoEverywhereSettings)
        {
            // Try to get the settings required for the notification check in
            try {
                DoNotificationCheckIn(octoEverywhereSettings.PrinterKey(), octoEverywhereSettings.PluginVersion(), IsConnectedViaOctoEverywhere())
            } catch (error) {
                LogError("DoNotificationCheckIn failed." + error);
            }
        }

        // We need to wait for the settings to be ready.
        // The SettingsViewModel is passed as the second param, because we list it as the second dependency in OCTOPRINT_VIEWMODELS
        self.settingsViewModel = parameters[1]
        self.onBeforeBinding = function() {
            // Set the settings and fire the callback.
            self.settings = self.settingsViewModel.settings;
            OnSettingsReady(self.settings.plugins.octoeverywhere);
        };
    }


     /* view model class, parameters for constructor, container to bind to
      * Please see http://docs.octoprint.org/en/master/plugins/viewmodels.html#registering-custom-viewmodels for more details
      * and a full list of the available options.
     */
     OCTOPRINT_VIEWMODELS.push({
         construct: OctoeverywhereViewModel,
         dependencies: ["wizardViewModel", "settingsViewModel"],
         elements: ["#wizard_plugin_octoeverywhere"]
     });
 });
