"""Configura o Alerta eBay passo a passo (rodado pelo CONFIGURAR.bat)."""
import secrets
import shutil
import string
import subprocess
import sys
import urllib.error
from pathlib import Path

import alerta

TAREFA = "Alerta eBay"
MINUTOS = 10


def preparar_dados():
    """Primeira vez: copia os arquivos de exemplos/ para dados/ (os seus, fora do GitHub)."""
    alerta.DADOS.mkdir(exist_ok=True)
    copiados = []
    for exemplo in sorted((alerta.PASTA / "exemplos").glob("*")):
        destino = alerta.DADOS / exemplo.name
        if not destino.exists():
            shutil.copy(exemplo, destino)
            copiados.append(exemplo.name)
    if copiados:
        print(f"\n  Criei a pasta dados com os exemplos ({', '.join(copiados)}).")
        print("  Troque pelos seus modelos e custos antes de usar de verdade.")


def perguntar(texto, atual=None):
    dica = f" [Enter = manter {atual}]" if atual else ""
    r = input(f"{texto}{dica}: ").strip()
    return r or atual or ""


def sim(texto):
    return not input(f"{texto} (S/N) [S]: ").strip().upper().startswith("N")


def criar_tarefa():
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = pythonw if pythonw.exists() else Path(sys.executable)
    script = alerta.PASTA / "alerta.py"
    ps = (
        f"$a = New-ScheduledTaskAction -Execute '{exe}' -Argument '\"{script}\"' "
        f"-WorkingDirectory '{alerta.PASTA}'; "
        f"$t = New-ScheduledTaskTrigger -Once -At (Get-Date) "
        f"-RepetitionInterval (New-TimeSpan -Minutes {MINUTOS}); "
        "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-StartWhenAvailable -MultipleInstances IgnoreNew "
        "-ExecutionTimeLimit (New-TimeSpan -Minutes 5); "
        f"Register-ScheduledTask -TaskName '{TAREFA}' -Action $a -Trigger $t -Settings $s "
        "-Description 'Avisa no celular anuncios do eBay no preco-alvo (Loja-Site\\ebay-alertas)' "
        "-Force | Out-Null"
    )
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True)
    if r.returncode != 0:
        print("Não consegui criar a tarefa automática:\n" + (r.stderr or r.stdout))
        return False
    return True


def main():
    print("=" * 60)
    print(" Configurar o Alerta eBay")
    print("=" * 60)
    preparar_dados()
    cfg = alerta.ler_json(alerta.CONFIG, {}) or {}

    # 1. Chaves da API
    print("\nPASSO 1 - Chaves da API do eBay")
    print("  Em developer.ebay.com > Application Keys, coluna Production.")
    while True:
        cfg["app_id"] = perguntar("  App ID (Client ID)", cfg.get("app_id"))
        dica = " [Enter = manter o já salvo]" if cfg.get("cert_id") else ""
        cfg["cert_id"] = input(f"  Cert ID (Client Secret){dica}: ").strip() or cfg.get("cert_id", "")
        print("  Testando as chaves...")
        try:
            alerta.token(cfg, {})
            print("  OK, o eBay aceitou as chaves.")
            break
        except urllib.error.HTTPError as e:
            print(f"  O eBay recusou ({e.code}): {e.read().decode('utf-8', 'replace')[:200]}")
            print("  Confira se copiou as chaves de Production (não Sandbox).")
        except Exception as e:
            print(f"  Falhou: {e}")
        if not sim("  Tentar de novo?"):
            return

    # 2. CEP nos EUA (o filtro de garantia do eBay exige um)
    print("\nPASSO 2 - CEP (ZIP Code) do endereço nos EUA para onde o eBay entrega")
    print("  (personal shopper ou redirecionadora).")
    print("  Serve para o eBay calcular o frete e liberar a Garantia de Autenticidade.")
    cfg["cep_eua"] = perguntar("  ZIP", cfg.get("cep_eua") or alerta.CEP_PADRAO)

    # 3. Aviso no celular
    print("\nPASSO 3 - Aviso no celular (app ntfy, grátis)")
    if not cfg.get("ntfy_topico"):
        letras = string.ascii_lowercase + string.digits
        cfg["ntfy_topico"] = "alerta-ebay-" + "".join(secrets.choice(letras) for _ in range(10))
    print("  1) Instale o app 'ntfy' no celular (Play Store ou App Store).")
    print("  2) Abra, toque no + e digite este tópico (exatamente assim):")
    print(f"\n        {cfg['ntfy_topico']}\n")
    print("  3) Toque em Inscrever / Subscribe.")
    input("  Quando terminar, aperte Enter para eu mandar um teste...")
    alerta.gravar_json(alerta.CONFIG, cfg)
    while True:
        try:
            alerta.enviar(cfg, "Teste do Alerta eBay", "Se você está lendo isto, o aviso funciona.")
        except Exception as e:
            print(f"  Não consegui mandar o teste: {e}")
        if sim("  Chegou a notificação no celular?"):
            break
        print(f"  Confira se o tópico no app está igual a: {cfg['ntfy_topico']}")
        if not sim("  Mandar outro teste?"):
            break

    # 4. Primeira busca
    print("\nPASSO 4 - Primeira busca (o resumo vai para o celular)")
    estado = alerta.ler_json(alerta.ESTADO, {})
    estado.pop("iniciado", None)  # reconfigurou: manda o resumo de novo
    alerta.gravar_json(alerta.ESTADO, estado)
    alerta.principal()

    # 5. A verificação automática roda na nuvem (GitHub Actions, a cada 30 min).
    #    Ligar também a tarefa do PC faria cada aviso chegar duas vezes;
    #    criar_tarefa() fica para o caso de largar a nuvem.
    print("\nPASSO 5 - A verificação automática roda na nuvem do GitHub (a cada 30 min).")
    print("  Se mudou o Cert ID, rode ENVIAR-CHAVE-GITHUB.bat para a nuvem usar o novo.")
    print("\nTudo certo. Para mudar produtos, tamanhos ou preços, edite dados\\produtos.csv"
          " e depois rode ENVIAR-PARA-NUVEM.bat.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelado.")
