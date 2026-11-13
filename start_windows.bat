@echo off
rem Clientela no Windows. O venv e .venv-win (nao .venv): se a pasta do app
rem estiver sincronizada com o Mac, o .venv de la e de macOS e nao roda aqui.
rem data/ compartilhado: ver data_dir.json no CLAUDE.md.
setlocal
cd /d "%~dp0"
if not exist ".venv-win\Scripts\python.exe" (
  echo Criando ambiente Python em .venv-win...
  py -3 -m venv .venv-win || python -m venv .venv-win || goto :erro
  ".venv-win\Scripts\python.exe" -m pip install -r requirements.txt || goto :erro
)
start "" http://127.0.0.1:8000
".venv-win\Scripts\python.exe" app.py
goto :eof

:erro
echo Nao consegui preparar o ambiente. Instale o Python 3.10 ou mais novo em python.org
echo (na instalacao, marque "Add python.exe to PATH") e abra este arquivo de novo.
start "" https://www.python.org/downloads/windows/
pause
