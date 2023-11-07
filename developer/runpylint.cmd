@echo off
REM This is just for local dev help, the github workflow does the real checkin linting.
pylint ..\octoeverywhere\
pylint ..\octoprint_octoeverywhere\
pylint ..\moonraker_octoeverywhere\