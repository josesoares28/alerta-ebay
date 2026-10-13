@echo off
cd /d "%~dp0"
gh workflow run alerta.yml -R josesoares28/alerta-ebay -f resumo=true
echo Pedido enviado: em 1 a 2 minutos chega no celular a lista completa (um aviso por modelo).
pause
