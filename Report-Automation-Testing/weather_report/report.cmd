@echo off
REM Weather Campaign Opportunity report - runs the full pipeline.
REM Usage:  report [--use-llm] [--report-date YYYY-MM-DD] [--skip-sales] ...
py "%~dp0src\run_report.py" %*
