@echo off
call "%~dp0venv\Scripts\activate.bat"
python "%~dp0migrate.py" %*
