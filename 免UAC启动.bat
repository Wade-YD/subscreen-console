@echo off
schtasks /Run /TN TouchToggle
if errorlevel 1 (
  echo Scheduled task "TouchToggle" not found. See README.md to register it first.
  pause
)
