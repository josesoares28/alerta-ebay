"""Leva para a nuvem (GitHub) as suas mudanças. É o que o ENVIAR-PARA-NUVEM.bat roda.

- Os seus dados (pasta dados/: produtos, ajustes, vendas e preços da Droper) vão
  num segredo do repositório, DADOS_PRIVADOS, que ninguém consegue ler. O
  repositório é público, então esses arquivos nunca entram no git.
- O resto (código, textos) vai pelo git.
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
    texto = base64.b64encode(buf.getvalue()).decode()
    if len(texto) > LIMITE:
        sys.exit(f"Os dados passaram do limite de um segredo do GitHub ({len(texto) // 1024} KB de 48 KB).")
    return texto


def enviar_dados():
    r = subprocess.run(["gh", "secret", "set", "DADOS_PRIVADOS", "-R", REPOSITORIO],
                       input=pacote(), text=True, capture_output=True)
    if r.returncode:
        sys.exit("FALHOU ao enviar os dados: " + (r.stderr or r.stdout).strip() + " Avise o Claude.")
    print("Dados enviados (produtos, ajustes, vendas e preços da Droper).")


def git(*args):
    return subprocess.run(["git", *args], cwd=PASTA).returncode


def enviar_codigo():
    git("add", "-A")
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=PASTA).returncode:
        git("commit", "-q", "-m", "Atualiza o alerta")
    if git("pull", "--rebase", "-q") or git("push", "-q"):
        sys.exit("FALHOU ao enviar o código. Avise o Claude.")
    print("Código e textos em dia no GitHub.")


if __name__ == "__main__":
    print("Enviando para a nuvem...")
    enviar_dados()
    enviar_codigo()
    print("Pronto. A nuvem usa a versão nova a partir da próxima rodada.")
