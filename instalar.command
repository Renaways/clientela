#!/bin/bash
# Instalador do Clientela no Mac: dois cliques no Finder.
# Cria o ambiente Python (.venv), instala os pacotes, gera o Clientela.app e abre o app.
# Rodar de novo é seguro: só atualiza os pacotes (o Clientela.app já existente fica).
set -e
cd "$(dirname "$0")"

echo "== Clientela: instalação =="

# O python3 do macOS (/usr/bin) é 3.9: procura um 3.10+ (Homebrew, python.org) antes de desistir.
PY=""
for c in python3.14 python3.13 python3.12 python3.11 python3.10 /opt/homebrew/bin/python3 /usr/local/bin/python3 \
         /Library/Frameworks/Python.framework/Versions/Current/bin/python3 python3; do
  p="$(command -v "$c" 2>/dev/null || true)"
  if [ -n "$p" ] && "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then PY="$p"; break; fi
done
if [ -z "$PY" ]; then
  echo
  echo "Preciso do Python 3.10 ou mais novo."
  echo "Baixe em https://www.python.org/downloads/macos/ (ou: brew install python),"
  echo "depois abra este instalador de novo."
  open "https://www.python.org/downloads/macos/" 2>/dev/null || true
  read -r -p "Aperte Enter para fechar."
  exit 1
fi
echo "Python: $("$PY" --version)"

if [ -x .venv/bin/python ] && ! .venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
  echo "O .venv existente é de um Python antigo: recriando."
  rm -rf .venv
fi
if [ ! -x .venv/bin/python ]; then
  echo "Criando o ambiente Python em .venv…"
  "$PY" -m venv .venv
fi
echo "Instalando os pacotes (pode levar um minuto)…"
.venv/bin/python -m pip install --upgrade pip -q
.venv/bin/python -m pip install -r requirements.txt -q

# Regerar o .app faz o macOS esquecer a permissão de Acessibilidade já dada: só na primeira vez.
if [ ! -d Clientela.app ]; then
  echo "Gerando o Clientela.app…"
  bash deploy/build_app.sh
fi

echo
echo "Pronto. Daqui em diante, abra o Clientela pelo Clientela.app nesta pasta."
echo "O FFmpeg e as outras ferramentas opcionais aparecem no primeiro uso, com o comando para instalar."
open Clientela.app
