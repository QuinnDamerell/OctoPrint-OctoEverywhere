@echo off
REM This is just for local dev help, the github workflow does the real checkin linting.
REM we must cd into the root and run, otherwise pylint will not find the pylintrc file.
cd ..
echo Running pylint on octoeverywhere
pylint .\octoeverywhere\

echo Running pylint on octoprint_octoeverywhere
pylint .\octoprint_octoeverywhere\
echo Running pylint on moonraker_octoeverywhere
pylint .\moonraker_octoeverywhere\
echo Running pylint on bambu_octoeverywhere
pylint .\bambu_octoeverywhere\
echo Running pylint on elegoo_octoeverywhere
pylint .\elegoo_octoeverywhere\

echo Running pylint on py_installer
pylint .\py_installer\
echo Running pylint on linux_host
pylint .\linux_host\

cd developer