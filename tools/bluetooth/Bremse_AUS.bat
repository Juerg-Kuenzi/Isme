@echo off
rem Bremse LOESEN - Doppelklick
cd /d "%~dp0"
py silentwind_reader.py --bremse aus --ja
timeout /t 5
