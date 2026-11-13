#!/bin/bash
cd "$(dirname "$0")"

if lsof -ti:8000 >/dev/null 2>&1; then
  echo "Clientela já está rodando em http://127.0.0.1:8000 — abrindo o navegador."
  open http://127.0.0.1:8000
  exit 0
fi

echo "Iniciando Clientela em http://127.0.0.1:8000 ..."
echo "(deixe esta janela aberta enquanto usa o app; feche-a ou aperte Ctrl+C para parar)"
( sleep 1.5 && open http://127.0.0.1:8000 ) &
exec .venv/bin/python app.py
