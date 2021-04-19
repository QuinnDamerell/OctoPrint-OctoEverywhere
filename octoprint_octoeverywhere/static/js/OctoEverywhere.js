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
        function ReportLocalFrontendPort(port, fullUrl)
        {
            OctoELog("Local frontend port found ["+port+"] reporting to backend. "+ fullUrl)
            const xhr = new XMLHttpRequest();
            xhr.onload = () => {
                if (xhr.status > 299) {
                    OctoELog("Failed to report frontend port to OctoEverywhere API. " + port)
                }
            };
            const payload = {
                "command":"setFrontendLocalPort",
                "port": port,
                "url": fullUrl
            };
            xhr.open('POST', '/api/plugin/octoeverywhere');
            xhr.setRequestHeader('Content-Type', 'application/json');
            xhr.send(JSON.stringify(payload));
        }

        function DetermineHostnameIsLocalAndReport(hostname, port, fullUrl)
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
                ReportLocalFrontendPort(port, fullUrl);
                return;
            }

            // Detect .local addresses
            // Check for the domain name suffix
            if(hostname.endsWith(".local"))
            {
                OctoELog("Current hostname detected as a .local domain. "+hostname);
                ReportLocalFrontendPort(port, fullUrl);
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
                ReportLocalFrontendPort(port, fullUrl);
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
                if(url.startsWith("https"))
                {
                    DetermineHostnameIsLocalAndReport(hostname, 443, url);
                }
                else
                {
                    DetermineHostnameIsLocalAndReport(hostname, 80, url);
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
            DetermineHostnameIsLocalAndReport(hostname, port, url);   
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
        // FindAndReportLocalFrontendPort("https://[2001:0db8:85a3:0000:0000:8a2e:0370:7334]/test");
        // FindAndReportLocalFrontendPort("https://[2001:0db8:85a3:0000:0000:8a2e:0370:7334]:555/test");
        // FindAndReportLocalFrontendPort("http://[2001:0db8:85a3:0000:0000:8a2e:0370:7334]:78945");
    }

     /* view model class, parameters for constructor, container to bind to
      * Please see http://docs.octoprint.org/en/master/plugins/viewmodels.html#registering-custom-viewmodels for more details
      * and a full list of the available options.
     */
     OCTOPRINT_VIEWMODELS.push({
         construct: OctoeverywhereViewModel,
         dependencies: ["wizardViewModel"],
         elements: ["#wizard_plugin_octoeverywhere"]
     });
 });
