/*
 * View model for octoeverywhere
 *
 * Author: Quinn Damerell
 * License: AGPLv3
 */
$(function() {
    function OctoeverywhereViewModel(parameters) {
        var self = this;

        // Used for the settings page to get the URL
        self.printerURL = ko.observable()

        // Used by the wizard to get the printer id.
        self.onWizardDetails = function (response) {
            if (response.octoeverywhere.details.AddPrinterUrl){
                self.printerURL(response.octoeverywhere.details.AddPrinterUrl)
            }
        };

        // Used by the py code to popup UI messages for various things.
        self.onDataUpdaterPluginMessage = function (plugin, data) {
            // check if it's for us.
            if (plugin !== "octoeverywhere_ui_popup_msg"){
                return
            }
            // Show a notification.
            new PNotify({
                'title': data.title,
                'text':  data.text,
                'type':  data.type,
                'hide':  data.autoHide,
                'delay': 10000,
                'mouseReset' : true
            });
        }

        // ;)
        console.log("***********************************")
        console.log("   Hello From OctoEverywhere! 🚀   ")
        console.log("***********************************")

        function OctoELog(text)
        {
            console.log("OctoEverywhere: "+text)
        }

        //
        // Index Session Validation
        //
        // What's this?
        //
        // Due to the caching used to speed up page loading, the logic that detects non-logged in users will break.
        // To combat this, immediately when this page loads we will do a request for a standard OctoPrint API. If we get
        // an unauthorized error code back, we will redirect to login. This will make the login page load a bit longer,
        // since the user will have to bounce through this page first. But that one time hit is worth it to make things load
        // faster the rest of the time.
        function CheckForUserAuth()
        {
            doRedirect = function()
            {
                OctoELog("Unauthed session detected. Redirecting to login.");
                window.location.href = "/login/?isFromOe=true"
            };

            // This API will test for auth, but has a small response and latency.
            $.ajax({
                url: "/api/printerprofiles",
                type: "GET",
                success: function (_) {},
                statusCode: {
                    403: function (_) {
                        doRedirect();
                    },
                    401: function (_) {
                        doRedirect();
                    }
                }
            });
        }
        // As of OctoPrint 1.7.3, on startup is called after the connection has been established and the passive user login has
        // been attempted, but before the settings load is called, which will fail if the user isn't logged in. It's important
        // to not check for user auth before onStartup, because the user auth state depends on the passive login call the start-up
        // process makes.
        self.onStartup = function() 
        {
            CheckForUserAuth();
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
            OctoELog("Local frontend port found [port:"+port+" isHttps:"+isHttps+" url:"+fullUrl+"] reporting to backend.")
            const xhr = new XMLHttpRequest();
            xhr.onload = () => {
                if (xhr.status > 299) {
                    OctoELog("Failed to report frontend port to OctoEverywhere API. " + port)
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
                OctoELog("Current hostname detected as IPV6. "+hostname);
                ReportLocalFrontendPort(port, isHttps, fullUrl);
                return;
            }

            // Detect .local addresses
            // Check for the domain name suffix
            if(hostname.endsWith(".local"))
            {
                OctoELog("Current hostname detected as a .local domain. "+hostname);
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
                OctoELog("Current hostname detected as a IPv4. "+hostname);
                ReportLocalFrontendPort(port, isHttps, fullUrl);
                return;
            }

            // We don't think this address is local.
            OctoELog("Current hostname isn't detected as a local URL "+hostname)
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
                OctoELog("No protocol could be found in url "+ url)
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
                OctoELog("Hostname parse failed. hostnameEnd "+ hostnameEnd + " protocolEnd "+protocolEnd+" url "+ url);
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
                OctoELog("Failed to parse port from hostname. "+hostname)
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
        function IsConnectedViaOctoEverywhere()
        {
            // Start with a to lower case to remove complexity.
            url = window.location.href.toLowerCase();

            // Check if the URL contains our domain name.
            // If so, we know we are loaded via our service.
            return url.indexOf(".octoeverywhere.com") != -1;
        }
        function DetectOctoEverywhereLoadedIndexAndInjectionHelpers()
        {
            // Only if we are connected via OctoEverywhere, inject the service connection helpers.
            if(IsConnectedViaOctoEverywhere())
            {
                OctoELog("OctoEverywhere based loading detected.");
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
                            OctoELog("Failed to call api/plugin/checkin; "+response.Status);
                            return;
                        }
                        // If there's a notification, fire it.
                        if(response.Result.Notification !== undefined && response.Result.Notification !== null)
                        {
                            new PNotify({
                                'title': response.Result.Notification.Title,
                                'text':  response.Result.Notification.Message,
                                'type':  response.Result.Notification.Type,
                                'hide':  response.Result.Notification.AutoHide,
                                'delay': response.Result.Notification.ShowForMs,
                                'mouseReset' : response.Result.Notification.MouseReset
                            });
                        }
                    }
                    catch (error)
                    {
                        OctoELog("Exception in DoNotificationCheckIn; "+error)
                    }
                },
                failed: function(error){
                    OctoELog("Failed to call plugin check in API "+error);
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
                OctoELog("DoNotificationCheckIn failed." + error);
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
