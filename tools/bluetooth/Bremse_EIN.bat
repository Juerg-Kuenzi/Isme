@echo off
rem Bremse EINSCHALTEN - Doppelklick
cd /d "%~dp0"
py silentwind_reader.py --bremse ein --ja
timeout /t 5
