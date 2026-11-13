-- Fonte do Clientela.app. Nao edite o bundle gerado; edite aqui e rode
-- deploy/build_app.sh, que troca __PROJECT__ pelo caminho real do projeto.
--
-- E um applet "stay-open": enquanto o servidor roda, o app fica vivo no Dock.
-- Isso importa por dois motivos:
--   1. o macOS atribui a permissao de Acessibilidade ao "processo responsavel"
--      pelo Python, que e este applet enquanto ele existir — se o applet
--      morresse, o Python ficaria orfao e o TCC deixaria de reconhecer a
--      concessao;
--   2. reabrir o app (duplo clique / clique no Dock) cai em `on reopen` em vez
--      de virar o dialogo "Clientela.app is not responding" que um bundle de
--      shell script provoca.

property projectPath : "__PROJECT__"
property appURL : "http://127.0.0.1:8000"

on serverRunning()
	try
		do shell script "lsof -ti tcp:8000 -sTCP:LISTEN"
		return true
	on error
		return false
	end try
end serverRunning

on run
	if not serverRunning() then
		-- Pede Acessibilidade em nome deste bundle na primeira vez; depois de
		-- concedida a chamada responde True e nao mostra nada.
		do shell script "cd " & quoted form of projectPath & " && mkdir -p data/logs && .venv/bin/python -c 'from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt; AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})' >/dev/null 2>&1; nohup .venv/bin/python app.py >> data/logs/launcher.log 2>&1 &"
		delay 1.5
	end if
	open location appURL
end run

on reopen
	open location appURL
end reopen

on idle
	-- Se o servidor caiu por fora (stop.command, crash), o applet sai junto.
	if not serverRunning() then quit
	return 10
end idle

on quit
	do shell script "kill $(lsof -ti tcp:8000 -sTCP:LISTEN) 2>/dev/null; true"
	continue quit
end quit
