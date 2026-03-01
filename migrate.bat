@echo off
call "%~dp0venv\Scripts\activate.bat"
python "%~dp0spotify_crossref.py" %*
