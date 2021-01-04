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
