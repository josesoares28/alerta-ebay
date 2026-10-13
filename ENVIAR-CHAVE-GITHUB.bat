@echo off
cd /d "%~dp0"
echo Enviando o Cert ID do eBay (de config.json) para os segredos do GitHub...
python -c "import json,subprocess,sys; c=json.load(open('config.json',encoding='utf-8'))['cert_id']; r=subprocess.run(['gh','secret','set','EBAY_CERT_ID','-R','josesoares28/alerta-ebay'],input=c,text=True); sys.exit(r.returncode)"
if errorlevel 1 (echo FALHOU. Avise o Claude.) else (echo Pronto: chave enviada. && gh workflow run alerta.yml -R josesoares28/alerta-ebay -f resumo=true && echo Primeira rodada na nuvem disparada: em 1 a 2 minutos chega no celular a lista completa. Pode voltar para o Claude.)
pause
