/*
 * View model for octoeverywhere
 *
 * Author: Quinn Damerell
 * License: AGPLv3
 */
$(function() {
     function OctoeverywhereViewModel(parameters) {
         var self = this;

         self.printerURL = ko.observable()

         self.onWizardDetails = function (response) {
             if (response.octoeverywhere.details.AddPrinterUrl){
                 self.printerURL(response.octoeverywhere.details.AddPrinterUrl)
             }
         };
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
