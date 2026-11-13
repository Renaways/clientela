#!/bin/bash
# Gera o `Clientela.app` na raiz do projeto a partir de deploy/launcher.applescript.
#
# Por que existe: o macOS atribui a permissao de Acessibilidade (que o pynput
# precisa para a auto-pausa por inatividade) ao "processo responsavel" — e para
# um script aberto pelo Terminal esse responsavel e o Terminal.app inteiro.
# Dar Acessibilidade ao Terminal abre a porta para qualquer coisa rodada nele.
# Lancado por este bundle, o responsavel passa a ser o proprio Clientela.app e
# a permissao fica restrita a ele. Veja o cabecalho do .applescript.
#
# O bundle grava o caminho absoluto do projeto; rode de novo se mover a pasta.
# Nao mova o .app depois de dar a permissao — o TCC amarra a concessao ao caminho.
set -euo pipefail

PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$PROJECT/Clientela.app"
SRC="$PROJECT/deploy/launcher.applescript"
TMP="$(mktemp -t clientela-launcher).applescript"

sed "s|__PROJECT__|$PROJECT|" "$SRC" > "$TMP"
rm -rf "$APP"
# -s: stay-open (mantem o applet vivo para os handlers idle/reopen/quit).
osacompile -s -o "$APP" "$TMP"
rm -f "$TMP"

/usr/libexec/PlistBuddy -c "Add :CFBundleIdentifier string com.clientela.launcher" "$APP/Contents/Info.plist"

# Assinatura ad-hoc: sem ela o TCC pode esquecer a concessao entre execucoes.
codesign --force --sign - "$APP" >/dev/null 2>&1 || echo "aviso: codesign falhou, seguindo sem assinatura"

echo "Gerado: $APP"
echo "Abra-o uma vez e conceda Acessibilidade ao 'Clientela' no dialogo que aparecer."
echo "Se o Clientela ja constava na lista de Acessibilidade, remova a entrada antiga (-) antes."
