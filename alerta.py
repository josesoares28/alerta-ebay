"""Alerta eBay: avisa no celular quando aparece no eBay um tênis que dá lucro na Droper.

Lê a lista de modelos (dados/produtos.csv) e procura no eBay, pela API oficial
(Browse API), anúncios "Comprar já" com Garantia de Autenticidade nos tamanhos de
cada linha. Para não confundir modelos parecidos, cada anúncio só passa se o
código do modelo (Style Code, ex.: DD1391-100) bater, no título ou na ficha do
anúncio; sem código, o título precisa ter as palavras de "exigir".

O lucro é calculado tamanho a tamanho: custo em reais (preço + frete pelo dólar
do Banco Central, spread do cartão, IOF e custo fixo por par, em dados/ajustes.txt)
contra a mediana das últimas vendas na Droper no mesmo tamanho e estado
(dados/vendas-droper.csv). Só avisa o que deixa o lucro mínimo.

Uso:
    python alerta.py           verifica e avisa no celular (é o que a nuvem roda)
    python alerta.py --lista   só mostra na tela o que está no alvo agora
    python alerta.py --resumo  manda para o celular a lista completa, um aviso por modelo
"""
import base64
import csv
import json
import os
import random
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

PASTA = Path(__file__).resolve().parent
DADOS = PASTA / "dados"  # seus dados: ficam só no PC; a nuvem recebe por um segredo do GitHub
PRODUTOS = DADOS / "produtos.csv"
AJUSTES = DADOS / "ajustes.txt"
PRECOS_DROPER = DADOS / "precos-droper.csv"
VENDAS_DROPER = DADOS / "vendas-droper.csv"
CONFIG = PASTA / "config.json"  # chaves do eBay e tópico do celular (fora do GitHub)
ESTADO = PASTA / "estado.json"  # anúncios já avisados e consultas guardadas (fora do GitHub)
LOG = PASTA / "log.txt"
NA_NUVEM = bool(os.environ.get("GITHUB_ACTIONS"))  # rodando no GitHub, cujo histórico é público
VERSAO = 3  # entra na chave dos produtos: subir manda um resumo novo de tudo

API = "https://api.ebay.com"
CATEGORIA_TENIS = "15709"  # Men's Athletic Shoes, onde ficam os pares adultos
CATEGORIA_FEMININO = "95672"  # Women's Athletic Shoes, para os modelos femininos
CEP_PADRAO = "19720"
DIAS_LEMBRAR = 90  # depois disso um anúncio já avisado é esquecido
BAIXOU_MINIMO = (10, 0.03)  # "Baixou!" só a partir de US$ 10 e 3% (câmbio e centavos não avisam)
DIAS_FICHA = 30  # Style Code lido da ficha fica guardado esse tempo
HORAS_GRUPO = (4, 8)  # anúncio de vários tamanhos é reaberto depois de 4 a 8 h (sorteado, para espalhar)
LIMITE_DIA = 4800  # o eBay libera 5.000 chamadas/dia para a Browse API

# Código de modelo Nike/Jordan: 2 letras + 4 dígitos + 3 dígitos (DD1391-100).
SKU_RE = re.compile(r"\b([A-Z]{2}\d{4})[-\s]?(\d{3})\b", re.I)

# Tamanho US masculino -> BR na Droper (tabela Nike/Jordan).
TAM_BR = {"8.5": "40", "9": "40.5", "9.5": "41", "10": "42", "10.5": "42.5",
          "11": "43", "11.5": "43.5", "12": "44"}


# Desconto no preço da Droper conforme quantas vendas o modelo (no estado, 40-44)
# teve nos últimos 30 dias: (a partir de N vendas, desconto).
DESCONTO_GIRO = [(4, 0.0), (2, 0.05), (1, 0.10), (0, 0.15)]

# Anúncio abaixo desta fração do que o par vale na Droper (em dólar) é suspeito:
# os achados de verdade ficam entre 35% e 60%; abaixo de 30% eram outro modelo
# (até chuteira de nome parecido), réplica ou conta nova sem avaliação.
PRECO_SUSPEITO = 0.30


def tam_br(tamanho, feminino=False):
    """Tamanho US do anúncio -> BR. O feminino (W) é 1,5 acima do masculino: W 10 = 8,5 = BR 40."""
    if not tamanho:
        return None
    return TAM_BR.get(fmt_num(float(tamanho) - (1.5 if feminino else 0)))

# Valores de exemplo (os mesmos de exemplos/ajustes.txt); os seus ficam em dados/ajustes.txt.
AJUSTES_PADRAO = {
    "custo_personal_shopper_reais": 400,
    "droper_voce_recebe_porcentagem": 87,
    "cartao_acima_do_dolar_oficial_porcentagem": 3.7,
    "iof_porcentagem": 3.5,
    "sales_tax_eua_porcentagem": 0,
    "aceita_oferta_ate_acima_do_alvo_porcentagem": 10,
    "lucro_minimo_reais": 500,
    "lucro_minimo_porcentagem_do_custo": 25,
}

URL_PTAX = ("https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/CotacaoDolarPeriodo("
            "dataInicial=@dataInicial,dataFinalCotacao=@dataFinalCotacao)?@dataInicial='{}'"
            "&@dataFinalCotacao='{}'&$format=json&$select=cotacaoVenda,dataHoraCotacao")

# Título com cara de par infantil, peça avulsa ou réplica: ignora.
EXCLUIR = re.compile(
    r"\b(td|ps|gs|toddlers?|kids?|preschool|infant|crib|baby|youth|grade school|"
    r"\d{1,2}(\.5)?\s?[cy]|box only|only box|empty box|no shoes|laces? only|insoles?|"
    r"replica|reps?|ua|unauthorized|custom)\b",
    re.I,
)

# Par infantil que só a descrição ou o departamento da ficha entregam
# (ex.: título e ficha de adulto, descrição "Size is Children's 10.5").
INFANTIL = re.compile(
    r"\b(child(?:ren)?[’']?s?|kids?[’']?s?\s+(?:size|sz)|toddlers?|pre-?school|grade\s*school|"
    r"little\s+kids?|big\s+kids?|youth|boys|girls|infants?|\d{1,2}(?:\.5)?[cy])\b",
    re.I,
)
DEPTO_INFANTIL = re.compile(r"\b(kids?|boys?|girls?|baby|toddlers?|youth|child(?:ren)?|infants?)\b", re.I)

CONDICOES = {
    "new with box": "novo com caixa",
    "new without box": "novo sem caixa",
    "new with defects": "novo com defeito",
    "new": "novo",
    "pre-owned - excellent": "usado - excelente",
    "pre-owned - good": "usado - bom",
    "pre-owned - fair": "usado - razoável",
    "pre-owned": "usado",
    "used": "usado",
}


# ---------------------------------------------------------------- utilidades

def log(msg, detalhe=False):
    """Grava no log.txt e mostra na tela. Detalhe (modelos, links) não sai na nuvem,
    porque o histórico das rodadas do GitHub é público."""
    if detalhe and NA_NUVEM:
        return
    linha = f"{datetime.now():%d/%m %H:%M:%S} {msg}"
    try:
        if LOG.exists() and LOG.stat().st_size > 500_000:
            LOG.write_text("\n".join(LOG.read_text(encoding="utf-8").splitlines()[-2000:]) + "\n",
                           encoding="utf-8")
        with LOG.open("a", encoding="utf-8") as f:
            f.write(linha + "\n")
    except OSError:
        pass
    if sys.stdout:
        try:
            print(linha)
        except Exception:
            pass


def usd(v):
    return "US$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def brl(v):
    return ("-" if v < 0 else "") + "R$ " + f"{abs(v):,.0f}".replace(",", ".")


def txt_lucro(v):
    return f"lucro ~{brl(v)}" if v >= 0 else f"prejuízo ~{brl(-v)}"


def numero(texto):
    """'8,5' -> 8.5 ; 'US$ 400,00' -> 400.0 ; '' -> None"""
    t = re.sub(r"[^\d,.]", "", str(texto or ""))
    if not t:
        return None
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    return float(t)


def fmt_num(v):
    return f"{v:.2f}".rstrip("0").rstrip(".")


def ler_json(caminho, padrao):
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return padrao


def gravar_json(caminho, dados):
    tmp = caminho.with_suffix(".tmp")
    tmp.write_text(json.dumps(dados, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(caminho)


def http(url, dados=None, cabecalhos=None, timeout=30):
    req = urllib.request.Request(url, data=dados, headers=cabecalhos or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


# ---------------------------------------------------------------- produtos

def termos_re(texto, sep):
    """'high, mid' -> regex que acha qualquer um, como palavra inteira."""
    partes = []
    for t in texto.split(sep):
        t = t.strip().lower()
        if t:
            partes.append((r"\b" if t[0].isalnum() else "") + re.escape(t)
                          + (r"\b" if t[-1].isalnum() else ""))
    return re.compile("|".join(partes), re.I) if partes else None


def skus_em(texto):
    return {(a + b).upper() for a, b in SKU_RE.findall(texto or "")}


def ler_texto(caminho):
    bruto = caminho.read_bytes()
    try:
        return bruto.decode("utf-8-sig")
    except UnicodeDecodeError:
        return bruto.decode("cp1252")  # salvo pelo Excel ou Bloco de Notas antigo


def ler_ajustes():
    aj = dict(AJUSTES_PADRAO)
    try:
        texto = ler_texto(AJUSTES)
    except OSError:
        log("dados/ajustes.txt não encontrado: uso os valores de exemplo")
        return aj
    for linha in texto.splitlines():
        if "=" in linha and not linha.strip().startswith("#"):
            chave, valor = linha.split("=", 1)
            if chave.strip() in aj and numero(valor) is not None:
                aj[chave.strip()] = numero(valor)
    return aj


def ler_droper():
    """(sku, tamanho BR) -> menor preço novo/usado na Droper, de precos-droper.csv."""
    tabela = {}
    try:
        texto = ler_texto(PRECOS_DROPER)
    except OSError:
        return tabela
    for l in csv.DictReader(texto.splitlines(), delimiter=";"):
        for sku in skus_em(l.get("sku")):
            tabela[(sku, (l.get("tam_br") or "").strip())] = {
                "novo": numero(l.get("novo")), "usado": numero(l.get("usado")),
                "lido": (l.get("lido_em") or "").strip()}
    # Mediana de cada modelo/estado: um tamanho com um anúncio só, pedindo o
    # dobro dos outros, não pode inflar o lucro.
    for sku in {s for s, _ in tabela}:
        for estado_par in ("novo", "usado"):
            precos = [v[estado_par] for (s, _), v in tabela.items()
                      if s == sku and isinstance(v, dict) and v[estado_par]]
            tabela[(sku, f"mediana_{estado_par}")] = statistics.median(precos) if len(precos) >= 3 else None
    return tabela


def ler_vendas():
    """Vendas de verdade na Droper (Market+ > Análise por Drop), de vendas-droper.csv.

    (sku, tamanho BR, 'novo'|'usado') -> mediana das últimas vendas e quantas vendas;
    (sku, 'modelo', estado) -> o mesmo para o modelo todo no 40-44, para o tamanho
    que vendeu pouco.
    """
    tabela = {}
    try:
        texto = ler_texto(VENDAS_DROPER)
    except OSError:
        return tabela
    for l in csv.DictReader(texto.splitlines(), delimiter=";"):
        estado_par = (l.get("estado") or "").strip().lower()
        mediana = numero(l.get("mediana_ultimas5"))
        if estado_par not in ("novo", "usado") or not mediana:
            continue
        for sku in skus_em(l.get("sku")):
            tabela[(sku, (l.get("tam_br") or "").strip(), estado_par)] = {
                "mediana": mediana, "n180": int(numero(l.get("vendas_180d")) or 0),
                "n30": int(numero(l.get("vendas_30d")) or 0), "lido": (l.get("lido_em") or "").strip()}
    por_tamanho = list(tabela.items())
    for sku, estado_par in {(s, e) for (s, _, e) in tabela}:
        faixa = [v for (s, t, e), v in por_tamanho
                 if s == sku and e == estado_par and 40 <= (numero(t) or 0) <= 44]
        if sum(v["n180"] for v in faixa) >= 3:
            tabela[(sku, "modelo", estado_par)] = {
                "mediana": statistics.median(v["mediana"] for v in faixa),
                "n180": sum(v["n180"] for v in faixa), "n30": sum(v["n30"] for v in faixa)}
    return tabela


def dolar(estado):
    """PTAX de venda do Banco Central (o último dia útil), uma consulta por dia."""
    hoje = datetime.now()
    guardado = estado.get("dolar") or {}
    if guardado.get("dia") == hoje.strftime("%Y-%m-%d"):
        return guardado["valor"]
    try:
        url = URL_PTAX.format((hoje - timedelta(days=10)).strftime("%m-%d-%Y"), hoje.strftime("%m-%d-%Y"))
        cotacoes = http(url).get("value") or []
        valor = float(cotacoes[-1]["cotacaoVenda"])
        estado["dolar"] = {"dia": hoje.strftime("%Y-%m-%d"), "valor": valor,
                           "cotacao_de": cotacoes[-1]["dataHoraCotacao"][:10]}
        return valor
    except Exception as e:
        log(f"não consegui o dólar do Banco Central ({type(e).__name__}); uso o último guardado")
        return guardado.get("valor")


def ler_produtos():
    texto = ler_texto(PRODUTOS)
    sep = ";" if texto.count(";") >= texto.count(",") else ","
    produtos = []
    for i, l in enumerate(csv.DictReader(texto.splitlines(), delimiter=sep), start=2):
        l = {(k or "").strip().lower(): (v or "").strip() for k, v in l.items()}
        if not (l.get("busca") or l.get("sku")) or l.get("ativo", "S").upper().startswith("N"):
            continue
        tmin, tmax = numero(l.get("tam_min")), numero(l.get("tam_max"))
        tamanhos = []
        if tmin is not None:
            t = tmin
            while t <= (tmax if tmax is not None else tmin) + 0.01:
                tamanhos.append(fmt_num(t))
                t += 0.5
        skus = skus_em(l.get("sku"))
        exigir = [termos_re(g, "/") for g in l.get("exigir", "").split(",")]
        produtos.append({
            "linha": i,
            "nome": l.get("nome") or l.get("busca") or l["sku"],
            "busca": l.get("busca", ""),
            "skus": skus,
            "exigir": [r for r in exigir if r],
            "excluir": termos_re(l.get("excluir", ""), ","),
            "tamanhos": tamanhos,
            "max_novo": numero(l.get("max_novo")),
            "max_usado": numero(l.get("max_usado")),
            "garantia": not l.get("so_com_garantia", "S").upper().startswith("N"),
            "feminino": l.get("feminino", "N").upper().startswith("S"),
            # muda quando o produto vigiado muda (não quando muda o preço)
            "chave": "|".join([f"v{VERSAO}", l.get("busca", "").lower(), *sorted(skus)]),
        })
    return produtos


# ---------------------------------------------------------------- eBay

_TOKEN_NUVEM = {}  # na nuvem o token do eBay fica só na memória: o estado vai para o cache de um repositório público


def token(cfg, estado):
    guarda = _TOKEN_NUVEM if NA_NUVEM else estado
    t = guarda.get("token") or {}
    if t.get("valor") and t.get("expira", 0) > datetime.now().timestamp() + 300:
        return t["valor"]
    chave = base64.b64encode(f"{cfg['app_id']}:{cfg['cert_id']}".encode()).decode()
    r = http(
        f"{API}/identity/v1/oauth2/token",
        dados=urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "scope": "https://api.ebay.com/oauth/api_scope",
        }).encode(),
        cabecalhos={"Authorization": f"Basic {chave}",
                    "Content-Type": "application/x-www-form-urlencoded"},
    )
    guarda["token"] = {"valor": r["access_token"],
                       "expira": datetime.now().timestamp() + int(r.get("expires_in", 7200))}
    return r["access_token"]


def cabecalhos_api(cfg, estado):
    cep = cfg.get("cep_eua") or CEP_PADRAO
    return {
        "Authorization": f"Bearer {token(cfg, estado)}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        "X-EBAY-C-ENDUSERCTX": "contextualLocation=" + urllib.parse.quote(f"country=US,zip={cep}"),
    }


class LimiteDoDia(Exception):
    pass


def api_get(url, cfg, estado):
    """GET na API do eBay contando as chamadas do dia (o limite é 5.000)."""
    hoje = datetime.now().strftime("%Y-%m-%d")
    cont = estado.get("chamadas") or {}
    if cont.get("dia") != hoje:
        cont = {"dia": hoje, "n": 0}
    if cont["n"] >= LIMITE_DIA:
        raise LimiteDoDia()
    cont["n"] += 1
    estado["chamadas"] = cont
    try:
        return http(url, cabecalhos=cabecalhos_api(cfg, estado))
    except urllib.error.HTTPError as e:
        if e.code == 429:  # a cota do dia acabou (conta tudo que usa a mesma chave, PC e nuvem)
            raise LimiteDoDia() from e
        raise


def buscar(prod, cfg, estado):
    """Anúncios do eBay que podem servir para este produto (sem checar preço por condição).

    Faz uma busca pelas palavras e uma por código de modelo: muito anúncio só
    tem o código na ficha, e muito anúncio sem ficha só tem as palavras.
    """
    cep = cfg.get("cep_eua") or CEP_PADRAO
    teto = max(v for v in (prod["max_novo"], prod["max_usado"]) if v is not None)
    teto = round(teto * (1 + prod.get("margem_oferta", 0)), 2)  # folga p/ quem aceita oferta
    filtros = [f"price:[..{fmt_num(teto)}]", "priceCurrency:USD",
               "deliveryCountry:US", f"deliveryPostalCode:{cep}"]
    if prod["garantia"]:
        filtros.append("qualifiedPrograms:{AUTHENTICITY_GUARANTEE}")
    base = {"filter": ",".join(filtros), "sort": "newlyListed", "limit": "200"}
    if prod["tamanhos"]:
        categoria = CATEGORIA_FEMININO if prod.get("feminino") else CATEGORIA_TENIS
        base["category_ids"] = categoria
        base["aspect_filter"] = (f"categoryId:{categoria},"
                                 f"US Shoe Size:{{{'|'.join(prod['tamanhos'])}}}")
    itens = {}
    for q in [prod["busca"]] + sorted(f"{s[:6]}-{s[6:]}" for s in prod["skus"]):
        if not q:
            continue
        lidos = 0
        for pagina in range(3):
            params = dict(base, q=q, offset=str(pagina * 200))
            r = api_get(f"{API}/buy/browse/v1/item_summary/search?" + urllib.parse.urlencode(params),
                        cfg, estado)
            for w in r.get("warnings") or []:
                log(f"aviso do eBay ({prod['nome']}): {w.get('message')}", detalhe=True)
            for it in r.get("itemSummaries") or []:
                itens.setdefault(it["itemId"], it)
                lidos += 1
            if lidos >= int(r.get("total") or 0) or not r.get("itemSummaries"):
                break
    return list(itens.values())


def enxuto(v):
    """De cada tamanho do anúncio, só o que o programa usa (o estado.json não incha)."""
    e = {k: v[k] for k in ("itemId", "legacyItemId", "title", "price", "conditionId", "condition",
                           "buyingOptions", "itemCreationDate", "itemOriginDate") if k in v}
    if "localizedAspects" in v:
        e["localizedAspects"] = [{"name": a.get("name"), "value": a.get("value")}
                                 for a in v["localizedAspects"] or []
                                 if re.search(r"size|style|mpn|department", a.get("name") or "", re.I)]
    e["shippingOptions"] = [{"shippingCost": o["shippingCost"]}
                            for o in v.get("shippingOptions") or [] if o.get("shippingCost")]
    vend = v.get("seller") or {}
    e["seller"] = {k: vend.get(k) for k in ("username", "feedbackPercentage", "feedbackScore")}
    e["image"] = {"imageUrl": (v.get("image") or {}).get("imageUrl")}
    m = INFANTIL.search(v.get("shortDescription") or "")
    e["shortDescription"] = m.group(0) if m else ""  # só importa se diz que é infantil
    return e


def variacoes(item, cfg, estado):
    """Anúncio com vários tamanhos: cada tamanho com seu preço.

    São muitos e mudam pouco, então ficam guardados de 4 a 8 horas; o anúncio é
    reaberto antes disso se o preço que a busca mostra para ele mudar.
    """
    grupos = estado.setdefault("grupos", {})
    href = item["itemGroupHref"]
    preco = (item.get("price") or {}).get("value")
    agora = datetime.now(timezone.utc)
    g = grupos.get(href)
    if g and g["preco"] == preco and datetime.fromisoformat(g["ate"]) > agora:
        return g["itens"]
    itens = [enxuto(v) for v in api_get(href, cfg, estado).get("items") or []
             if (v.get("estimatedAvailabilities") or [{}])[0].get("estimatedAvailabilityStatus") != "OUT_OF_STOCK"]
    grupos[href] = {"preco": preco, "itens": itens,
                    "ate": (agora + timedelta(hours=random.uniform(*HORAS_GRUPO))).isoformat()}
    return itens


def ficha(item, cfg, estado):
    """Style Code e tamanho que o vendedor pôs na ficha do anúncio ('' se não pôs),
    e se a descrição curta ou o departamento dizem que o par é infantil.

    Anúncio de grupo já vem com a ficha; o resto custa uma consulta, guardada.
    """
    completo = item
    if item.get("localizedAspects") is None:
        fichas = estado.setdefault("fichas", {})
        guardada = fichas.get(item["itemId"])
        if guardada and "infantil" in guardada:
            return guardada
        if not item.get("itemHref"):
            return {"codigo": "", "tamanho": "", "infantil": False}
        completo = api_get(item["itemHref"], cfg, estado)
    aspectos = completo.get("localizedAspects") or []
    codigo = " ".join(a.get("value") or "" for a in aspectos
                      if (a.get("name") or "").lower() in ("style code", "style number", "mpn"))
    tam = next((a.get("value") or "" for a in aspectos
                if (a.get("name") or "").lower() == "us shoe size"), "")
    depto = next((a.get("value") or "" for a in aspectos
                  if (a.get("name") or "").lower() == "department"), "")
    m = re.search(r"\d{1,2}(?:\.5)?", tam)
    infantil = bool(INFANTIL.search(completo.get("shortDescription") or "") or DEPTO_INFANTIL.search(depto))
    dados = {"codigo": codigo, "tamanho": m.group(0) if m else "", "infantil": infantil}
    if completo is not item:
        estado["fichas"][item["itemId"]] = dict(dados, quando=datetime.now(timezone.utc).isoformat())
    return dados


def titulo_pode_ser(titulo, prod):
    """Só pelo título, sem gastar consulta: o anúncio ainda pode ser o modelo vigiado?

    Recusa o que modelo_confere recusaria de qualquer jeito: código de outro
    modelo no título, ou título sem a maioria dos grupos de "exigir".
    """
    no_titulo = skus_em(titulo)
    if prod["skus"] and no_titulo:
        return bool(no_titulo & prod["skus"])
    batem = sum(1 for r in prod["exigir"] if r.search(titulo))
    return batem >= ((len(prod["exigir"]) + 1) // 2 if prod["skus"] else len(prod["exigir"]))


def modelo_confere(item, prod, cfg, estado):
    """É mesmo o modelo vigiado? None = não; '' = sim; texto = sim, com um alerta.

    Código no título decide sozinho. Código só na ficha precisa da maioria dos
    grupos de "exigir" no título (barra tênis customizado ou outro modelo
    com a ficha copiada). Título com todas as palavras mas ficha de outro modelo
    passa com alerta, porque é comum o vendedor escolher a ficha errada.
    """
    titulo = item.get("title") or ""
    if not titulo_pode_ser(titulo, prod):
        return None  # nem a ficha salvaria: não abre
    batem = sum(1 for r in prod["exigir"] if r.search(titulo))
    todas = batem == len(prod["exigir"])
    maioria = batem >= (len(prod["exigir"]) + 1) // 2
    if prod["skus"]:
        no_titulo = skus_em(titulo)
        if no_titulo:
            return "" if no_titulo & prod["skus"] else None
        na_ficha = skus_em(ficha(item, cfg, estado)["codigo"])
        if na_ficha & prod["skus"]:
            return "" if maioria else None
        if na_ficha & prod.get("outros_skus", set()):
            return None  # ficha de outro modelo da lista: é aquele (ex.: duas versões do mesmo tênis)
        if na_ficha:
            codigos = ", ".join(f"{s[:6]}-{s[6:]}" for s in sorted(na_ficha))
            return (f"a ficha do anúncio diz {codigos}, "
                    "outro modelo: confira as fotos") if todas else None
    return "" if todas else None


def tamanho_do(item):
    for a in item.get("localizedAspects") or []:
        if "size" in (a.get("name") or "").lower():
            m = re.search(r"\d{1,2}(?:\.5)?", a.get("value") or "")
            if m:
                return m.group(0)
    t = item.get("title") or ""
    m = (re.search(r"(?:\bsize|\bsz|\bus|\bmen'?s)\s*[:#.\-]?\s*(\d{1,2}(?:\.5)?)\b", t, re.I)
         or re.search(r"\b(\d{1,2}(?:\.5)?)\s?m\b", t, re.I))
    return m.group(1) if m else None


def descartar(titulo, prod):
    return bool(EXCLUIR.search(titulo) or (prod["excluir"] and prod["excluir"].search(titulo)))


def avaliar(item, prod, de_grupo=False):
    """Devolve o anúncio já resumido se ele bate com o produto, senão None."""
    titulo = item.get("title") or ""
    if descartar(titulo, prod):
        return None
    if (item.get("price") or {}).get("currency", "USD") != "USD":
        return None
    preco = float(item["price"]["value"])
    cond_id = str(item.get("conditionId") or "")
    cond_en = (item.get("condition") or "").strip()
    novo = cond_id.startswith("1") if cond_id else cond_en.lower().startswith("new")
    if cond_id == "3010" or cond_en.lower() == "pre-owned - fair":
        return None  # usado gasto ("Fair"): revende mal
    teto = prod["max_novo"] if novo else prod["max_usado"]
    aceita_oferta = "BEST_OFFER" in (item.get("buyingOptions") or [])
    if teto is None:
        return None
    if preco > teto + 0.001 and not (
            aceita_oferta and preco <= teto * (1 + prod.get("margem_oferta", 0)) + 0.001):
        return None
    if prod["garantia"] and not de_grupo and "qualifiedPrograms" in item \
            and "AUTHENTICITY_GUARANTEE" not in (item.get("qualifiedPrograms") or []):
        return None
    tam = tamanho_do(item)
    if de_grupo and prod["tamanhos"]:
        if tam is None or fmt_num(float(tam)) not in prod["tamanhos"]:
            return None
        est = (item.get("estimatedAvailabilities") or [{}])[0]
        if est.get("estimatedAvailabilityStatus") == "OUT_OF_STOCK":
            return None
    frete = None
    for op in item.get("shippingOptions") or []:
        c = op.get("shippingCost")
        if c and c.get("currency", "USD") == "USD":
            frete = float(c["value"]) if frete is None else min(frete, float(c["value"]))
    partes = (item.get("itemId") or "").split("|")  # v1|<anúncio>|<variação>
    legado = item.get("legacyItemId") or (partes[1] if len(partes) > 1 else "")
    link = item.get("itemWebUrl")
    if legado:
        var = partes[2] if len(partes) > 2 and partes[2] not in ("", "0") else ""
        link = f"https://www.ebay.com/itm/{legado}" + (f"?var={var}" if var else "")
    vend = item.get("seller") or {}
    criado = item.get("itemCreationDate") or item.get("itemOriginDate")
    return {
        "id": item.get("itemId"),
        "produto": prod["nome"],
        "chave": prod["chave"],
        "titulo": titulo,
        "preco": preco,
        "teto": teto,
        "aceita_oferta": aceita_oferta,
        "acima_do_alvo": preco > teto + 0.001,  # só entrou porque aceita oferta
        "frete": frete,
        "novo": novo,
        "condicao": CONDICOES.get(cond_en.lower(), cond_en or ("novo" if novo else "usado")),
        "tamanho": tam,
        "feminino": prod.get("feminino", False),
        "link": link,
        "imagem": (item.get("image") or {}).get("imageUrl"),
        "vendedor": vend.get("username"),
        "feedback": vend.get("feedbackPercentage"),
        "avaliacoes": vend.get("feedbackScore"),
        "criado": criado,
    }


def achados(prod, cfg, estado):
    vistos_grupo = set()
    saida = []
    for item in buscar(prod, cfg, estado):
        if item.get("itemGroupHref"):
            titulo = item.get("title") or ""
            if (item["itemGroupHref"] in vistos_grupo or descartar(titulo, prod)
                    or not titulo_pode_ser(titulo, prod)):
                continue  # o título já reprova: não abre os tamanhos
            vistos_grupo.add(item["itemGroupHref"])
            candidatos = [(v, avaliar(v, prod, de_grupo=True)) for v in variacoes(item, cfg, estado)]
        else:
            candidatos = [(item, avaliar(item, prod))]
        # avaliar vem antes: é barato e evita abrir a ficha de anúncio caro demais
        for bruto, a in candidatos:
            if a:
                ressalva = modelo_confere(bruto, prod, cfg, estado)
                if ressalva is not None:
                    a["alerta"] = ressalva
                    a["_bruto"] = bruto  # a ficha (par infantil?) só é aberta se passar no lucro
                    if not a["tamanho"]:  # o lucro depende do tamanho
                        a["tamanho"] = ficha(bruto, cfg, estado)["tamanho"] or None
                    if prod["tamanhos"] and a["tamanho"] and fmt_num(float(a["tamanho"])) not in prod["tamanhos"]:
                        continue  # a ficha diz um tamanho fora da faixa
                    saida.append(a)
    return saida


# ---------------------------------------------------------------- lucro

def menor_anuncio(a, sku, droper):
    """Menor preço anunciado na Droper no tamanho e estado (precos-droper.csv)."""
    linha = droper.get((sku, a["tam_br"]))
    if not linha:
        return None
    a["droper_novo"] = linha["novo"]
    preco = linha["novo"] if a["novo"] else linha["usado"]
    mediana = droper.get((sku, "mediana_novo" if a["novo"] else "mediana_usado"))
    if preco and mediana and preco > 1.3 * mediana:
        a["droper_fora"] = preco  # anúncio isolado bem acima dos outros: vale a mediana
        return mediana
    return preco


def estimar_lucro(a, prod, droper, vendas, aj, cotacao):
    """Acrescenta ao anúncio: custo em reais, quanto vende na Droper, lucro e o alvo.

    Quanto vende: mediana das últimas vendas no tamanho (Market+); tamanho com
    menos de 2 vendas usa a mediana do modelo. Em par novo, se hoje já tem
    anúncio mais barato que isso na Droper, vale o anúncio (é com ele que vai
    concorrer). Alvo: o preço que ainda deixa o lucro mínimo, limitado ao teto
    da planilha.
    """
    a["tam_br"] = tam_br(a["tamanho"], prod.get("feminino"))
    a["lucro"] = a["lucro_oferta"] = a["droper"] = a["preco_max"] = None
    a["alvo"] = a["teto"]
    if not cotacao:
        return
    fator = (cotacao * (1 + aj["cartao_acima_do_dolar_oficial_porcentagem"] / 100)
             * (1 + aj["iof_porcentagem"] / 100) * (1 + aj["sales_tax_eua_porcentagem"] / 100))

    def custo(preco_usd):
        return (preco_usd + (a["frete"] or 0)) * fator + aj["custo_personal_shopper_reais"]

    a["custo"] = custo(a["preco"])
    if not a["tam_br"]:
        return
    estado_par = "novo" if a["novo"] else "usado"
    ref, fonte, giro = None, "", None
    for sku in sorted(prod["skus"]):
        no_tam = vendas.get((sku, a["tam_br"], estado_par))
        modelo = vendas.get((sku, "modelo", estado_par))
        if no_tam and no_tam["n180"] >= 2:
            ref, fonte, giro = no_tam["mediana"], "mediana das últimas vendas", no_tam
        elif modelo:
            ref, fonte, giro = modelo["mediana"], "mediana do modelo; pouca venda no tamanho", no_tam
        anuncio = menor_anuncio(a, sku, droper)
        if anuncio and (ref is None or (a["novo"] and anuncio < ref)):
            ref, fonte = anuncio, ("menor anúncio na Droper, mediana do modelo"
                                   if a.get("droper_fora") else "menor anúncio na Droper hoje")
        if ref:
            break
    if not ref:
        return
    a["droper"], a["droper_fonte"] = ref, fonte
    a["fracao_droper"] = a["preco"] / (ref / cotacao)
    a["vendas_30d"] = giro["n30"] if giro else 0
    # Modelo que vende pouco: para vender logo tem que pedir abaixo da mediana.
    modelo = vendas.get((sku, "modelo", estado_par)) or {}
    a["vendas_30d_modelo"] = modelo.get("n30", 0)
    a["desconto_giro"] = next(d for minimo, d in DESCONTO_GIRO if a["vendas_30d_modelo"] >= minimo)
    recebe = ref * (1 - a["desconto_giro"]) * aj["droper_voce_recebe_porcentagem"] / 100
    a["recebe"], a["lucro"] = recebe, recebe - a["custo"]
    # Até quanto pagar: sobra o lucro mínimo em reais e, em par caro, a % mínima do custo.
    custo_max = min(recebe - aj["lucro_minimo_reais"],
                    recebe / (1 + aj["lucro_minimo_porcentagem_do_custo"] / 100))
    a["preco_max"] = (custo_max - aj["custo_personal_shopper_reais"]) / fator - (a["frete"] or 0)
    a["alvo"] = min(a["teto"], a["preco_max"])
    a["acima_do_alvo"] = a["preco"] > a["alvo"] + 0.001
    if a["acima_do_alvo"] and a["alvo"] > 0:
        a["lucro_oferta"] = recebe - custo(a["alvo"])


def ressalvas(a, prod, cfg, estado):
    """Marca com ⚠ o que passou mas merece desconfiança (vai para o fim da lista).

    A ficha já foi aberta para ver se é infantil, então conferir o código dela não custa nada.
    """
    novas = []
    codigos = skus_em(ficha(a["_bruto"], cfg, estado)["codigo"])
    if not a.get("alerta") and codigos and not codigos & prod["skus"]:
        novas.append(f"a ficha do anúncio diz {', '.join(f'{s[:6]}-{s[6:]}' for s in sorted(codigos))}, "
                     "outro modelo: confira as fotos")
    if a.get("fracao_droper") is not None and a["fracao_droper"] < PRECO_SUSPEITO:
        novas.append(f"preço muito abaixo do normal ({a['fracao_droper']:.0%} do que vale na Droper): "
                     "pode ser outro modelo, réplica ou golpe")
    if novas:
        a["alerta"] = "; ".join([a["alerta"]] + novas if a.get("alerta") else novas)


def no_alvo(a, prod):
    """Preço até o alvo, ou até X% acima se o vendedor aceita oferta."""
    alvo = a.get("alvo")
    if alvo is None or alvo <= 0:
        return False
    if a["preco"] <= alvo + 0.001:
        return True
    return a["aceita_oferta"] and a["preco"] <= alvo * (1 + prod.get("margem_oferta", 0)) + 0.001


# ---------------------------------------------------------------- aviso

def enviar(cfg, titulo, mensagem, link=None, imagem=None, prioridade=4):
    corpo = {"topic": cfg["ntfy_topico"], "title": titulo, "message": mensagem,
             "tags": ["athletic_shoe"], "priority": prioridade}
    if link:
        corpo["click"] = link
        corpo["actions"] = [{"action": "view", "label": "Abrir no eBay", "url": link}]
    if imagem:
        corpo["attach"] = imagem
    for espera in (5, 15, None):  # a rede às vezes falha um instante: tenta até 3 vezes
        try:
            http("https://ntfy.sh/", dados=json.dumps(corpo).encode("utf-8"),
                 cabecalhos={"Content-Type": "application/json"})
            return
        except urllib.error.HTTPError:
            raise  # o ntfy respondeu (ex.: limite de envios): repetir agora não adianta
        except OSError:
            if espera is None:
                raise
            time.sleep(espera)


def linha_lucro(a):
    """Uma linha curta: lucro estimado ou por que não deu para estimar."""
    estado_pt = "novo" if a["novo"] else "usado"
    if a.get("lucro") is not None:
        giro = f"; {a['vendas_30d']} venda(s) em 30 dias no tamanho" if a.get("vendas_30d") else ""
        if a.get("desconto_giro"):
            giro += (f"; -{a['desconto_giro']:.0%} porque o modelo vendeu "
                     f"{a.get('vendas_30d_modelo', 0)} {estado_pt}(s) em 30 dias")
        texto = (f"{txt_lucro(a['lucro']).capitalize()}: Droper {a['tam_br']} {estado_pt} "
                 f"{brl(a['droper'])} ({a['droper_fonte']}{giro}) → você recebe {brl(a['recebe'])}; "
                 f"custo {brl(a['custo'])}")
        if a.get("droper_fora"):
            texto += f". No {a['tam_br']} só há anúncio a {brl(a['droper_fora'])}"
        if a.get("preco_max") and a["preco_max"] > 0:
            texto += f". Dá o lucro mínimo pagando até {usd(a['preco_max'])}"
        if a.get("lucro_oferta") is not None:
            texto += f". Fechando a oferta em {usd(a['alvo'])}: {txt_lucro(a['lucro_oferta'])}"
        return texto
    if not a.get("tam_br"):
        return "Lucro: tamanho não identificado, confira no anúncio"
    if a.get("droper_novo") and not a["novo"]:
        return (f"Lucro: sem usado na Droper no {a['tam_br']} (novo lá: {brl(a['droper_novo'])}); "
                f"custo {brl(a['custo'])}")
    if a.get("custo") is not None:
        return f"Lucro: sem preço na Droper no {a['tam_br']}; custo {brl(a['custo'])}"
    return "Lucro: sem o dólar do dia"


def tam_txt(a):
    """'10.5', ou '10.5W' em modelo feminino (com o BR, que é o que vende na Droper)."""
    if not a["tamanho"]:
        return ""
    if a.get("feminino"):
        return f"{a['tamanho']}W" + (f" (BR {a['tam_br']})" if a.get("tam_br") else "")
    return a["tamanho"]


def texto_aviso(a, preco_antes=None):
    tam = f" tam {tam_txt(a)}" if a["tamanho"] else ""
    titulo = f"{a['produto']}: {usd(a['preco'])} ({'novo' if a['novo'] else 'usado'}){tam}"
    if a.get("acima_do_alvo"):
        titulo += " · aceita oferta"
    if preco_antes:
        titulo = f"Baixou! {titulo}"
    linhas = []
    if a.get("alerta"):
        linhas.append(f"⚠ {a['alerta']}")
    if a.get("lucro") is not None or a.get("tam_br") is not None or a.get("custo") is not None:
        linhas.append(linha_lucro(a))
    if a.get("acima_do_alvo"):
        linhas.append(f"Acima do alvo ({usd(a['alvo'])}), mas aceita oferta: proponha ~{usd(a['alvo'])}")
    elif a.get("aceita_oferta"):
        linhas.append("Aceita oferta: dá para tentar menos")
    if preco_antes:
        linhas.append(f"Antes {usd(preco_antes)}")
    frete = "frete grátis" if a["frete"] == 0 else (f"frete {usd(a['frete'])}" if a["frete"] else "frete: ver anúncio")
    total = f" · total {usd(a['preco'] + (a['frete'] or 0))}" if a["frete"] else ""
    linhas.append(f"{a['condicao']} · {frete}{total}")
    if a["vendedor"]:
        nota = str(a["feedback"] or "?").replace(".", ",")
        linhas.append(f"Vendedor {a['vendedor']} · {nota}% ({a['avaliacoes'] or 0} avaliações)")
    if a["criado"]:
        try:
            quando = datetime.fromisoformat(a["criado"].replace("Z", "+00:00")).astimezone()
            linhas.append(f"Anunciado {quando:%d/%m %H:%M}")
        except ValueError:
            pass
    linhas.append(a["titulo"])
    return titulo, "\n".join(linhas)


def ordem_lucro(a):
    """Mais lucro primeiro; os com ⚠ e quem não tem lucro estimado vão para o fim."""
    return (bool(a.get("alerta")), a.get("lucro") is None, -(a.get("lucro") or 0), a["preco"])


def estado_curto(a):
    """'novo com caixa' é o normal e vira só 'novo'; o resto (usado - razoável,
    novo sem caixa...) aparece inteiro, porque mexe no preço de revenda."""
    c = a.get("condicao") or ""
    if a["novo"] and c in ("", "novo", "novo com caixa"):
        return "novo"
    return c or "usado"


def linha_resumo(a):
    partes = [("⚠ " if a.get("alerta") else "") + usd(a["preco"]), estado_curto(a)]
    if a["tamanho"]:
        partes.append(f"tam {tam_txt(a)}")
    if a.get("acima_do_alvo"):
        partes.append(f"aceita oferta: proponha {usd(a['alvo'])}")
    if a.get("lucro") is not None:
        partes.append(txt_lucro(a["lucro"]))
    if not a.get("avaliacoes"):
        partes.append("vendedor sem avaliações")
    if a.get("alerta"):
        partes.append(a["alerta"])
    return " · ".join(partes) + f"\n{a['link']}"


def enviar_resumo(cfg, prods, encontrados, por_mensagem=20):
    """Lista completa no celular: uma notificação por modelo, sem cortar nada.

    O app mostra a mais nova em cima, então manda do pior para o melhor modelo:
    o de maior lucro fica no topo e o "nada no alvo" lá embaixo.
    """
    vazios = [p["nome"] for p in prods if not any(a["chave"] == p["chave"] for a in encontrados)]
    if vazios:
        enviar(cfg, "Nada no alvo agora", ", ".join(vazios), prioridade=2)
    grupos = []
    for prod in prods:
        dele = sorted((a for a in encontrados if a["chave"] == prod["chave"]), key=ordem_lucro)
        if dele:
            grupos.append((prod, dele))
    grupos.sort(key=lambda g: ordem_lucro(g[1][0]), reverse=True)
    for prod, dele in grupos:
        partes = [dele[i:i + por_mensagem] for i in range(0, len(dele), por_mensagem)]
        for n, parte in reversed(list(enumerate(partes, 1))):
            titulo = f"{prod['nome']}: {len(dele)} no alvo"
            if len(partes) > 1:
                titulo += f" ({n}/{len(partes)})"
            topo = [prod["giro_txt"]] if n == 1 and prod.get("giro_txt") else []
            enviar(cfg, titulo, "\n".join(topo + [linha_resumo(a) for a in parte]), prioridade=2)


# ---------------------------------------------------------------- principal

def ler_config():
    """config.json no PC; na nuvem (GitHub) as mesmas chaves vêm de variáveis secretas."""
    cfg = ler_json(CONFIG, None) or {}
    for chave, var in (("app_id", "EBAY_APP_ID"), ("cert_id", "EBAY_CERT_ID"),
                       ("ntfy_topico", "NTFY_TOPICO"), ("cep_eua", "CEP_EUA")):
        if os.environ.get(var):
            cfg[chave] = os.environ[var]
    return cfg


def principal(so_listar=False, resumo=False):
    cfg = ler_config()
    if not cfg.get("app_id") or not cfg.get("cert_id"):
        log("Falta a chave do eBay: rode CONFIGURAR.bat (PC) ou ENVIAR-CHAVE-GITHUB.bat (nuvem)")
        return 0  # sem chave não é erro de rodada; não adianta falhar toda meia hora
    if not cfg.get("ntfy_topico"):
        log("Falta o tópico do celular (ntfy): rode CONFIGURAR.bat")
        return 0
    estado = ler_json(ESTADO, {})
    if NA_NUVEM:
        estado.pop("token", None)  # de rodadas antigas, quando ainda era guardado
    vistos = estado.setdefault("vistos", {})
    iniciados = estado.setdefault("iniciados", {})  # produto -> quando começou a ser vigiado
    agora = datetime.now(timezone.utc)
    encontrados, erros, estreantes, vigiados = [], [], [], []

    try:
        produtos = ler_produtos()
    except OSError as e:
        produtos, erros = [], [f"não consegui ler dados/produtos.csv ({e})"]
    aj, droper, vendas, cotacao = ler_ajustes(), ler_droper(), ler_vendas(), dolar(estado)
    todos_skus = set().union(*(p["skus"] for p in produtos))
    for prod in produtos:
        prod["outros_skus"] = todos_skus - prod["skus"]
        giro = {e: sum((vendas.get((s, "modelo", e)) or {}).get("n30", 0) for s in prod["skus"])
                for e in ("novo", "usado")}
        if any((s, "modelo", e) in vendas for s in prod["skus"] for e in giro):
            prod["giro_txt"] = (f"Droper vendeu {giro['novo']} novo(s) e {giro['usado']} usado(s) "
                                "em 30 dias (tam 40-44)")
    for prod in produtos:
        if prod["max_novo"] is None and prod["max_usado"] is None:
            log(f"linha {prod['linha']} ({prod['nome']}) sem preço máximo: pulei", detalhe=True)
            continue
        prod["margem_oferta"] = aj["aceita_oferta_ate_acima_do_alvo_porcentagem"] / 100
        try:
            achou = achados(prod, cfg, estado)
            for a in achou:
                estimar_lucro(a, prod, droper, vendas, aj, cotacao)
            # par infantil que só a descrição entrega: uma consulta por anúncio, guardada
            achou = [a for a in achou if no_alvo(a, prod) and not ficha(a["_bruto"], cfg, estado)["infantil"]]
            for a in achou:
                ressalvas(a, prod, cfg, estado)
            encontrados += achou
            vigiados.append(prod)
            if prod["chave"] not in iniciados:
                estreantes.append(prod)
            log(f"{prod['nome']}: {len(achou)} anúncio(s) no alvo", detalhe=True)
        except LimiteDoDia:
            log("o eBay não libera mais consultas hoje (5.000 por dia); volto quando renovar, "
                "às 4h da manhã (5h de novembro a março)")
            break
        except urllib.error.HTTPError as e:
            corpo = e.read().decode("utf-8", "replace")[:300]
            erros.append(f"{prod['nome']}: eBay respondeu {e.code} {corpo}")
            if e.code == 401:
                estado.pop("token", None)
                _TOKEN_NUVEM.pop("token", None)
        except Exception as e:  # rede fora do ar, etc.
            erros.append(f"{prod['nome']}: {type(e).__name__} {e}")

    for e in erros:
        log("ERRO " + (e.split(": ", 1)[-1] if NA_NUVEM else e))  # na nuvem, sem o nome do modelo

    if so_listar:
        for a in sorted(encontrados, key=lambda a: a["preco"]):
            t, m = texto_aviso(a)
            print(f"\n{t}\n{m}\n{a['link']}")
        print(f"\n{len(encontrados)} anúncio(s) no alvo agora.")
        gravar_json(ESTADO, estado)
        return 0

    # Lista completa no celular: pedida (--resumo) ou de produto recém-adicionado,
    # em vez de um aviso por anúncio.
    mostrar = vigiados if resumo else estreantes
    if mostrar:
        try:
            enviar_resumo(cfg, mostrar, encontrados)
        except Exception as e:
            log(f"ERRO ao avisar no celular: {e}")
            gravar_json(ESTADO, estado)
            return 1
        chaves = {p["chave"] for p in mostrar}
        for a in encontrados:
            if a["chave"] in chaves:
                vistos[a["id"]] = {"preco": a["preco"], "quando": agora.isoformat()}
        for p in mostrar:
            iniciados[p["chave"]] = agora.isoformat()

    novos, avisados = [], 0
    for a in encontrados:
        antes = vistos.get(a["id"])
        if antes is None:
            novos.append((a, None))
        elif a["preco"] <= antes["preco"] - max(BAIXOU_MINIMO[0], BAIXOU_MINIMO[1] * antes["preco"]):
            novos.append((a, antes["preco"]))  # quedas pequenas somam: compara com o último preço avisado

    for a, preco_antes in sorted(novos, key=lambda x: x[0]["preco"]):
        titulo, msg = texto_aviso(a, preco_antes)
        try:
            enviar(cfg, titulo, msg, a["link"], a["imagem"], prioridade=3 if a.get("alerta") else 4)
            vistos[a["id"]] = {"preco": a["preco"], "quando": agora.isoformat()}
            log(f"avisado: {titulo} {a['link']}", detalhe=True)
            avisados += 1
        except Exception as e:
            log(f"ERRO ao avisar no celular: {e}")
            break  # tenta de novo na próxima rodada

    # Avisa erro no celular no máximo 1 vez por dia, para não lotar.
    if erros:
        ultimo = estado.get("aviso_erro")
        if not ultimo or agora - datetime.fromisoformat(ultimo) > timedelta(hours=24):
            try:
                enviar(cfg, "Alerta eBay com problema",
                       "\n".join(erros)[:900] + "\n\nDetalhes: log.txt no PC ou aba Actions do GitHub",
                       prioridade=3)
                estado["aviso_erro"] = agora.isoformat()
            except Exception:
                pass

    limite = agora - timedelta(days=DIAS_LEMBRAR)
    estado["vistos"] = {k: v for k, v in vistos.items()
                        if datetime.fromisoformat(v["quando"]) > limite}
    limite_ficha = agora - timedelta(days=DIAS_FICHA)
    estado["fichas"] = {k: v for k, v in (estado.get("fichas") or {}).items()
                        if datetime.fromisoformat(v["quando"]) > limite_ficha}
    estado["grupos"] = {k: v for k, v in (estado.get("grupos") or {}).items()
                        if datetime.fromisoformat(v["ate"]) > agora}
    estado.pop("iniciado", None)  # formato antigo (antes de iniciar por produto)
    estado["ultima_rodada"] = agora.isoformat()
    log(f"{len(vigiados)} modelo(s) verificado(s), {len(encontrados)} anúncio(s) no alvo, "
        f"{avisados} aviso(s) novo(s){', resumo enviado' if mostrar else ''}; "
        f"consultas ao eBay hoje: {(estado.get('chamadas') or {}).get('n', 0)}")
    gravar_json(ESTADO, estado)
    return 1 if erros and not encontrados else 0


if __name__ == "__main__":
    sys.exit(principal(so_listar="--lista" in sys.argv, resumo="--resumo" in sys.argv))
