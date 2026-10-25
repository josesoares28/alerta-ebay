"""Leva para a nuvem (GitHub) as suas mudanças. É o que o ENVIAR-PARA-NUVEM.bat roda.

- Os seus dados (pasta dados/: produtos, ajustes, vendas e preços da Droper, Macs) vão
  num segredo do repositório, DADOS_PRIVADOS, que ninguém consegue ler. O
  repositório é público, então esses arquivos nunca entram no git.
- O resto (código, textos) vai pelo git.

Também liga e desliga as rodadas na nuvem: `python nuvem.py ligar` (LIGAR.bat) e
`python nuvem.py desligar` (DESLIGAR.bat).
"""
import base64
import io
import subprocess
import sys
import zipfile
from pathlib import Path

PASTA = Path(__file__).resolve().parent
DADOS = PASTA / "dados"
ARQUIVOS = ["produtos.csv", "ajustes.txt", "vendas-droper.csv", "precos-droper.csv"]
OPCIONAIS = ["macs.csv"]  # sem ele, a nuvem só não vigia Macs
REPOSITORIO = "josesoares28/alerta-ebay"
LIMITE = 48 * 1024  # tamanho máximo de um segredo do GitHub


def pacote():
    """Os arquivos de dados/ num zip, em texto (base64), do jeito que o segredo guarda."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for nome in ARQUIVOS:
            arq = DADOS / nome
            if not arq.exists():
                sys.exit(f"Falta dados/{nome}. Copie o de exemplos/ para dados/ e ajuste.")
            z.write(arq, nome)
        for nome in OPCIONAIS:
            if (DADOS / nome).exists():
                z.write(DADOS / nome, nome)
    texto = base64.b64encode(buf.getvalue()).decode()
    if len(texto) > LIMITE:
        sys.exit(f"Os dados passaram do limite de um segredo do GitHub ({len(texto) // 1024} KB de 48 KB).")
    return texto


def enviar_dados():
    r = subprocess.run(["gh", "secret", "set", "DADOS_PRIVADOS", "-R", REPOSITORIO],
                       input=pacote(), text=True, capture_output=True)
    if r.returncode:
        sys.exit("FALHOU ao enviar os dados: " + (r.stderr or r.stdout).strip() + " Avise o Claude.")
    print("Dados enviados (produtos, ajustes, vendas e preços da Droper, Macs).")


def git(*args):
    return subprocess.run(["git", *args], cwd=PASTA).returncode


def enviar_codigo():
    git("add", "-A")
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=PASTA).returncode:
        git("commit", "-q", "-m", "Atualiza o alerta")
    if git("pull", "--rebase", "-q") or git("push", "-q"):
        sys.exit("FALHOU ao enviar o código. Avise o Claude.")
    print("Código e textos em dia no GitHub.")


def gh_api(*args):
    return subprocess.run(["gh", "api", *args], capture_output=True, text=True)


def ligar():
    """Liga o workflow e começa a corrente: uma rodada agora e as próximas a cada ~30 min."""
    caminho = f"repos/{REPOSITORIO}/actions/workflows/alerta.yml"
    r = gh_api("-X", "PUT", f"{caminho}/enable")
    if r.returncode == 0:
        r = gh_api("-X", "POST", f"{caminho}/dispatches", "-f", "ref=main",
                   "-f", "inputs[continuar]=true")
    if r.returncode:
        sys.exit("FALHOU ao ligar: " + (r.stderr or r.stdout).strip() + " Avise o Claude.")
    print("Ligado: a primeira rodada começa agora e as próximas vêm a cada ~30 min.")


def desligar():
    """Desliga o workflow e cancela a rodada que estiver esperando a vez."""
    caminho = f"repos/{REPOSITORIO}/actions"
    r = gh_api("-X", "PUT", f"{caminho}/workflows/alerta.yml/disable")
    if r.returncode:
        sys.exit("FALHOU ao desligar: " + (r.stderr or r.stdout).strip() + " Avise o Claude.")
    r = gh_api(f"{caminho}/workflows/alerta.yml/runs?per_page=20",
               "-q", '.workflow_runs[] | select(.status != "completed") | .id')
    for rodada in r.stdout.split():
        gh_api("-X", "POST", f"{caminho}/runs/{rodada}/cancel")
    print("Pausado na nuvem.")


if __name__ == "__main__":
    comando = sys.argv[1] if len(sys.argv) > 1 else ""
    if comando == "ligar":
        ligar()
    elif comando == "desligar":
        desligar()
    else:
        print("Enviando para a nuvem...")
        enviar_dados()
        enviar_codigo()
        print("Pronto. A nuvem usa a versão nova a partir da próxima rodada.")
