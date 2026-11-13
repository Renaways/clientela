#!/bin/bash
# Encerra o Clientela aberto pelo Clientela.app (que nao tem janela de Terminal
# para dar Ctrl+C). Tambem funciona se ele foi aberto pelo start.command.
PIDS=$(lsof -ti tcp:8000 -sTCP:LISTEN)
if [ -z "$PIDS" ]; then
  echo "Clientela nao esta rodando."
else
  kill $PIDS && echo "Clientela encerrado."
fi
