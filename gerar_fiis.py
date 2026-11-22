#!/usr/bin/env python3
"""Gera fiis.json com todos os FIIs negociados na B3.

Fontes públicas (sem chave, sem custo):
  - B3, séries históricas COTAHIST: último preço, variação e volume de cada ticker (papel tipo FII = CODBDI 12).
  - CVM Dados Abertos, informe mensal de FII: nome, segmento, cotas emitidas, patrimônio líquido,
    valor patrimonial da cota, número de cotistas e dividend yield mensal.

Como cada indicador é calculado (mesmas convenções do Genesis Bolsa: razões em fração, null = não informado):
  preco            último fechamento do ticker (B3), sem ajuste por proventos
  variacao_dia     último fechamento ÷ fechamento anterior − 1
  vp_cota          valor patrimonial da cota no último informe mensal (CVM)
  pvp              preço ÷ vp_cota
  valor_mercado    preço × cotas emitidas
  rend_cota_12m    rendimento anual por cota: média dos rendimentos mensais válidos dos últimos 12 informes × 12
                   (rendimento do mês = DY do mês informado à CVM × VP da cota do mês; meses com valor negativo
                   ou acima de 5% do VP são erros de preenchimento do fundo e ficam de fora)
  dy               rend_cota_12m ÷ preço atual   (precisa de ao menos 3 meses válidos; senão null)
  segmento         Papel, Fundo de fundos ou, para tijolo, o segmento declarado à CVM (pela composição da carteira)
  vol_medio        volume financeiro médio por pregão nos últimos 60 pregões (B3)

Uso:  python gerar_fiis.py [--saida fiis.json] [--sem-cache]
"""
import argparse, csv, io, json, os, re, sys, time, unicodedata, zipfile
from datetime import datetime, timezone
from statistics import mean
from urllib.request import Request, urlopen

RAIZ = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(RAIZ, ".cache")
ANO = datetime.now().year
URL_COTAHIST = "https://bvmf.bmfbovespa.com.br/InstDados/SerHist/COTAHIST_A{ano}.ZIP"
URL_CVM = "https://dados.cvm.gov.br/dados/FII/DOC/INF_MENSAL/DADOS/inf_mensal_fii_{ano}.zip"
JANELA_VOLUME = 60          # pregões
ATIVO_ATE_DIAS = 15         # ticker precisa ter negociado nos últimos N pregões do mercado
UA = {"User-Agent": "Mozilla/5.0 (compatible; fiis-script/1.0)"}


def baixar(url, destino, max_idade_h=6, forcar=False):
    os.makedirs(CACHE, exist_ok=True)
    if not forcar and os.path.exists(destino) and (time.time() - os.path.getmtime(destino)) < max_idade_h * 3600:
        return destino
    print(f"  baixando {url}", flush=True)
    tmp = destino + ".part"
    ultimo_erro = None
    for tentativa in range(3):
        try:
            with urlopen(Request(url, headers=UA), timeout=120) as r, open(tmp, "wb") as f:
                while True:
                    bloco = r.read(1 << 20)
                    if not bloco:
                        break
                    f.write(bloco)
            os.replace(tmp, destino)
            return destino
        except Exception as e:  # rede instável: tenta de novo
            ultimo_erro = e
            time.sleep(3 * (tentativa + 1))
    raise RuntimeError(f"falha ao baixar {url}: {ultimo_erro}")


def num(v):
    try:
        x = float(str(v).replace(",", "."))
        return x if x == x else None
    except (TypeError, ValueError):
        return None


# ── B3: COTAHIST ─────────────────────────────────────────────────────────────
def ler_cotahist(caminho):
    """-> {ticker: {"isin", "nome", "dias": [(data, fechamento, volume)]}}"""
    papeis = {}
    with zipfile.ZipFile(caminho) as z:
        nome = z.namelist()[0]
        with z.open(nome) as f:
            for bruto in io.TextIOWrapper(f, encoding="latin1"):
                if len(bruto) < 245 or not bruto.startswith("01"):
                    continue
                if bruto[10:12] != "12" or bruto[24:27] != "010":   # CODBDI 12 = FII, mercado à vista (lote padrão)
                    continue
                ticker = bruto[12:24].strip()
                data = f"{bruto[2:6]}-{bruto[6:8]}-{bruto[8:10]}"
                fech = int(bruto[108:121]) / 100
                vol = int(bruto[170:188]) / 100
                isin = bruto[230:242].strip()
                p = papeis.setdefault(ticker, {"isin": isin, "nome": bruto[27:39].strip(), "dias": []})
                p["dias"].append((data, fech, vol))
    for p in papeis.values():
        p["dias"].sort()
    return papeis


# ── CVM: informe mensal ──────────────────────────────────────────────────────
def ler_csv_zip(z, trecho):
    nome = next(n for n in z.namelist() if trecho in n)
    with z.open(nome) as f:
        return list(csv.DictReader(io.TextIOWrapper(f, encoding="latin1"), delimiter=";"))


def ler_cvm(anos):
    geral, compl, apas = {}, {}, {}   # chave (cnpj, competencia) -> linha de maior versão
    for ano in anos:
        destino = baixar(URL_CVM.format(ano=ano), os.path.join(CACHE, f"cvm_fii_{ano}.zip"))
        with zipfile.ZipFile(destino) as z:
            for alvo, trecho, chave_data in ((geral, "geral", "Data_Referencia"), (compl, "complemento", "Data_Referencia"), (apas, "ativo_passivo", "Data_Referencia")):
                for r in ler_csv_zip(z, trecho):
                    k = (r["CNPJ_Fundo_Classe"], r[chave_data])
                    if k not in alvo or int(r.get("Versao") or 0) >= int(alvo[k].get("Versao") or 0):
                        alvo[k] = r
    return geral, compl, apas


SEGMENTOS = {
    "Títulos e Valores Mobiliários": "Papel (recebíveis)",
    "Hibrido": "Híbrido",
    "": "Outros",
}


def segmento(valor):
    v = (valor or "").strip()
    return SEGMENTOS.get(v, v or "Outros")


def isins_historicos(primeiro=2017):
    """ISIN -> CNPJs em todos os informes antigos da CVM (o ISIN costuma estar em branco nos mais recentes)."""
    mapa = {}
    for ano in range(primeiro, ANO - 1):
        try:
            destino = baixar(URL_CVM.format(ano=ano), os.path.join(CACHE, f"cvm_fii_{ano}.zip"), max_idade_h=24 * 30)
            with zipfile.ZipFile(destino) as z:
                for r in ler_csv_zip(z, "geral"):
                    isin = (r.get("Codigo_ISIN") or "").strip()
                    cnpj = r.get("CNPJ_Fundo_Classe") or r.get("CNPJ_Fundo")
                    if isin and cnpj:
                        mapa.setdefault(isin, set()).add(cnpj)
        except Exception as e:   # um ano indisponível não derruba o resto
            print(f"  aviso: informe {ano} ignorado ({e})")
    return mapa


def nome_curto(n):
    """"FII MAXI RENDA" -> "Maxi Renda"; siglas curtas ficam em maiúsculas."""
    n = (n or "").strip()
    for pre in ("FII ", "FI ", "FDO "):
        if n.upper().startswith(pre):
            n = n[len(pre):]
    out = []
    for t in n.split():
        sem_vogal = not any(c in "AEIOUÁÉÍÓÚÂÊÔÃÕ" for c in t.upper())
        romano = re.fullmatch(r"[IVX]+", t.upper()) is not None
        if t.lower() in ("da", "de", "do", "das", "dos", "e") and out:
            out.append(t.lower())
        else:
            out.append(t.upper() if (len(t) <= 2 or sem_vogal or romano) else t.capitalize())
    return " ".join(out) or n


RUIDO = re.compile(
    r"(?i)(?<![\wÀ-ú])(fundo\s+de\s+fundos\s+de\s+investimento\s+imobili[aá]rio|fundo\s+de\s+investimento\s+imobili[aá]rio|"
    r"fundo\s+de\s+investimento|fundo\s+imobili[aá]rio|f\.?i\.?i\.?|fi|fdo|"
    r"responsabilidade\s+limitada|respons\.?\s*limitada|resp\.?\s*ltda|resp\.?\s*limitada|rl)(?![\wÀ-ú])")


def nome_do_cvm(n):
    """"MAXI RENDA - FUNDO DE INVESTIMENTO IMOBILIÁRIO" -> "Maxi Renda" (None se sobrar pouco)."""
    limpo = re.sub(r"[-–.]+", " ", RUIDO.sub(" ", n or ""))
    limpo = re.sub(r"\s+", " ", limpo).strip()
    if len(limpo) < 3:
        return None
    return nome_curto(limpo)


def classificar(ap, seg_cvm):
    """Papel, fundo de fundos, tijolo (com o segmento da CVM) ou híbrido, pela carteira do último informe."""
    if not ap:
        return segmento(seg_cvm)
    g = lambda k: num(ap.get(k)) or 0.0
    papel = sum(g(k) for k in ("CRI", "CRI_CRA", "LCI", "LCI_LCA", "LIG", "Letras_Hipotecarias"))
    fof = g("FII")
    tijolo = g("Direitos_Bens_Imoveis") + g("Acoes_Sociedades_Atividades_FII") + g("Cotas_Sociedades_Atividades_FII")
    total = max(g("Total_Investido"), papel + fof + tijolo)
    if total <= 0:
        return segmento(seg_cvm)
    if fof / total >= 0.5:
        return "Fundo de fundos"
    if papel / total >= 0.5:
        return "Papel (recebíveis)"
    if tijolo / total >= 0.5:
        s = segmento(seg_cvm)
        return "Tijolo (diversificado)" if s in ("Multicategoria", "Outros", "Híbrido") else s
    return "Híbrido"


def _sem_acento(x):
    return re.sub(r"[^A-Z0-9 ]", " ", unicodedata.normalize("NFD", x.upper()).encode("ascii", "ignore").decode())


_GENERICAS = {"FUNDO", "DE", "DO", "DA", "DOS", "DAS", "E", "INVESTIMENTO", "INVESTIMENTOS", "FII", "FI", "FDO",
              "RESPONSABILIDADE", "LIMITADA", "RL", "RESP", "LTDA", "FIIS"}


def _tokens(x):
    return [t for t in _sem_acento(x).split() if t not in _GENERICAS]


def _casa(tb, tc):
    """O termo da B3 (ex.: "TIVIO", "RI") aparece no nome da CVM, inteiro, como prefixo ou como sigla de palavras seguidas."""
    pre = tb[: max(2, min(len(tb), 4))]
    if any(c.startswith(pre) for c in tc):
        return True
    n = len(tb)
    return n <= 4 and any("".join(w[0] for w in tc[i:i + n]) == tb for i in range(len(tc)))


def achar_por_nome(nome_b3, candidatos):
    """Para tickers sem ISIN na CVM: fundos cujo nome contém todos os termos do nome de pregão da B3.
    Exige ao menos um termo de 4+ letras (senão é só uma sigla e o risco de erro é alto)."""
    termos = _tokens(re.sub(r"^FII ", "", nome_b3.upper()))
    if not termos or not any(len(t) >= 4 for t in termos):
        return []
    return [c for c, tc in candidatos.items() if all(_casa(t, tc) for t in termos)]


def principal():
    ap = argparse.ArgumentParser()
    ap.add_argument("--saida", default=os.path.join(RAIZ, "fiis.json"))
    ap.add_argument("--sem-cache", action="store_true")
    args = ap.parse_args()
    if args.sem_cache and os.path.isdir(CACHE):
        for f in os.listdir(CACHE):
            os.remove(os.path.join(CACHE, f))

    print("B3: cotações", flush=True)
    papeis = ler_cotahist(baixar(URL_COTAHIST.format(ano=ANO), os.path.join(CACHE, f"cotahist_{ANO}.zip")))
    if not papeis:
        sys.exit("COTAHIST sem FIIs: formato mudou?")
    pregoes = sorted({d for p in papeis.values() for d, _, _ in p["dias"]})
    ultimo_pregao = pregoes[-1]
    corte = pregoes[-ATIVO_ATE_DIAS] if len(pregoes) >= ATIVO_ATE_DIAS else pregoes[0]

    print("CVM: informes mensais", flush=True)
    geral, compl, apas = ler_cvm([ANO - 1, ANO])
    # ISIN -> fundo: usa qualquer informe em que o ISIN apareça (alguns meses vêm em branco)
    por_isin, nome_cnpj = {}, {}
    ultimo_geral = {}
    for (c, m), g in geral.items():
        if c not in ultimo_geral or m > ultimo_geral[c][0]:
            ultimo_geral[c] = (m, g)
        isin = (g.get("Codigo_ISIN") or "").strip()
        if isin:
            por_isin[isin] = c
        nome_cnpj[c] = (g.get("Nome_Fundo_Classe") or "").upper()
    por_isin = {i: (c, ultimo_geral[c][1]) for i, c in por_isin.items()}
    # ISIN que só aparece em informes antigos: usa o CNPJ que ainda informa em 2025/2026 (o mais recente)
    for isin, cnpjs in isins_historicos().items():
        if isin in por_isin:
            continue
        vivos = [c for c in cnpjs if c in ultimo_geral]
        if vivos:
            c = max(vivos, key=lambda x: ultimo_geral[x][0])
            por_isin[isin] = (c, ultimo_geral[c][1])

    # competências por fundo (para o DY de 12 meses)
    meses_por_cnpj = {}
    for (c, m) in compl:
        meses_por_cnpj.setdefault(c, []).append(m)

    # um ticker por ISIN (o mais negociado)
    escolhidos = {}
    for t, p in papeis.items():
        if not p["dias"] or p["dias"][-1][0] < corte:
            continue
        vol = sum(v for _, _, v in p["dias"][-JANELA_VOLUME:])
        atual = escolhidos.get(p["isin"])
        if atual is None or vol > atual[1]:
            escolhidos[p["isin"]] = (t, vol)

    usados = {por_isin[i][0] for i in escolhidos if i in por_isin}
    recente = max(m for (_, m) in geral)
    candidatos_livres = {
        c: _tokens(g.get("Nome_Fundo_Classe") or "")
        for c, (m, g) in ultimo_geral.items()
        if c not in usados and m >= recente[:5] + "01-01" and (g.get("Mercado_Negociacao_Bolsa") or "") == "S"
    }

    fiis, sem_cvm = [], []
    for isin, (ticker, _) in escolhidos.items():
        p = papeis[ticker]
        dias = p["dias"]
        preco = dias[-1][1]
        anterior = dias[-2][1] if len(dias) > 1 else None
        vol_medio = mean(v for _, _, v in dias[-JANELA_VOLUME:])
        reg = por_isin.get(isin)
        item = {
            "ticker": ticker, "nome": nome_curto(p["nome"]), "nome_completo": None, "segmento": "Outros", "preco": round(preco, 2),
            "variacao_dia": round(preco / anterior - 1, 4) if anterior else None,
            "vol_medio": round(vol_medio), "vp_cota": None, "pvp": None, "dy": None, "rend_cota_12m": None,
            "ultimo_rendimento_cota": None, "valor_mercado": None, "patrimonio_liquido": None,
            "cotas_emitidas": None, "cotistas": None, "mandato": None, "gestao": None,
            "administrador": None, "informe_ref": None, "dy_meses": 0, "cvm": False,
        }
        if not reg:   # sem ISIN na CVM: tenta pelo nome do fundo (só candidatos livres, negociados em bolsa)
            cands = achar_por_nome(p["nome"], candidatos_livres)
            validos = []
            for c in cands:
                m_ult = max((m for (cc, m) in compl if cc == c), default=None)
                vp_c = num(compl[(c, m_ult)].get("Valor_Patrimonial_Cotas")) if m_ult else None
                if vp_c and 0.3 <= preco / vp_c <= 2.5:   # preço compatível com o valor patrimonial
                    validos.append(c)
            if len(validos) == 1:
                reg = (validos[0], ultimo_geral[validos[0]][1])
                candidatos_livres.pop(validos[0], None)
        if not reg:
            sem_cvm.append(ticker)
            fiis.append(item)
            continue
        cnpj, g = reg
        item["nome_completo"] = (g.get("Nome_Fundo_Classe") or "").strip().title() or None
        item["cnpj"] = cnpj
        item["cvm"] = True
        item["nome"] = nome_do_cvm(g.get("Nome_Fundo_Classe")) or item["nome"]
        item["mandato"] = (g.get("Mandato") or "").strip() or None
        item["gestao"] = (g.get("Tipo_Gestao") or "").strip() or None
        item["administrador"] = (g.get("Nome_Administrador") or "").strip().title() or None

        meses = sorted(meses_por_cnpj.get(cnpj, []))
        ap_ult = None
        for m in sorted((m for (c, m) in apas if c == cnpj), reverse=True)[:1]:
            ap_ult = apas[(cnpj, m)]
        item["segmento"] = classificar(ap_ult, g.get("Segmento_Atuacao"))
        if meses:
            ult = compl[(cnpj, meses[-1])]
            vp = num(ult.get("Valor_Patrimonial_Cotas"))
            cotas = num(ult.get("Cotas_Emitidas")) or num(g.get("Quantidade_Cotas_Emitidas"))
            pl = num(ult.get("Patrimonio_Liquido"))
            item["informe_ref"] = meses[-1][:7]
            item["vp_cota"] = round(vp, 2) if vp else None
            pvp = preco / vp if vp and vp > 0 else None
            if pvp is not None and not (0.2 <= pvp <= 3.5):   # preço e valor patrimonial incompatíveis: dado suspeito
                pvp, vp = None, None
                item["cvm"] = False
            item["vp_cota"] = round(vp, 2) if vp else None
            item["pvp"] = round(pvp, 2) if pvp else None
            item["patrimonio_liquido"] = round(pl) if pl else None
            item["cotas_emitidas"] = round(cotas) if cotas else None
            item["valor_mercado"] = round(preco * cotas) if cotas else None
            cot = num(ult.get("Total_Numero_Cotistas"))
            item["cotistas"] = round(cot) if cot else None
            rends = []
            for m in meses[-12:]:
                c = compl[(cnpj, m)]
                dy_m, vp_m = num(c.get("Percentual_Dividend_Yield_Mes")), num(c.get("Valor_Patrimonial_Cotas"))
                cotas_m = num(c.get("Cotas_Emitidas"))
                rd = num((apas.get((cnpj, m)) or {}).get("Rendimentos_Distribuir"))
                if dy_m is not None and vp_m and 0 < dy_m <= 0.05:   # DY do mês informado pelo fundo
                    rends.append(dy_m * vp_m)
                elif rd and cotas_m and vp_m and 0 < rd / cotas_m <= 0.05 * vp_m:
                    rends.append(rd / cotas_m)   # DY inválido: usa os rendimentos a distribuir no fim do mês, por cota
                elif dy_m == 0 or rd == 0:
                    rends.append(0.0)            # fundo informou que não distribuiu no mês
            item["dy_meses"] = len(rends)
            if rends:
                item["ultimo_rendimento_cota"] = round(rends[-1], 4)
            if len(rends) >= 3 and preco > 0:
                anual = mean(rends) * 12
                item["rend_cota_12m"] = round(anual, 4)
                dy = anual / preco
                item["dy"] = round(dy, 4) if 0.001 < dy <= 0.3 else None   # ~0 = fundo sem rendimentos informados   # fora disso é dado suspeito
        fiis.append(item)

    # só entram fundos com dados completos: preço, P/VP e dividend yield calculados
    total_antes = len(fiis)
    fiis = [x for x in fiis if x["pvp"] is not None and x["dy"] is not None]
    excluidos = total_antes - len(fiis)
    fiis.sort(key=lambda x: x["ticker"])
    saida = {
        "gerado_em": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fonte": {
            "nome": "B3 (COTAHIST) e CVM Dados Abertos (informe mensal de FII)",
            "cotahist_ultimo_pregao": ultimo_pregao,
            "cvm_informe_mais_recente": max((m for _, m in compl), default=None),
            "janela_volume_pregoes": JANELA_VOLUME,
            "metodo_dy": "média dos rendimentos mensais válidos por cota (últimos 12 informes) × 12 ÷ preço atual",
        },
        "fiis": fiis,
    }
    os.makedirs(os.path.dirname(args.saida), exist_ok=True)
    with open(args.saida, "w", encoding="utf-8") as f:
        json.dump(saida, f, ensure_ascii=False, separators=(",", ":"))
    com_dy = sum(1 for x in fiis if x["dy"] is not None)
    com_pvp = sum(1 for x in fiis if x["pvp"] is not None)
    print(f"OK: {len(fiis)} FIIs completos ({excluidos} fora por dados insuficientes) | pregão {ultimo_pregao} | com DY {com_dy} | com P/VP {com_pvp} | sem dados CVM {len(sem_cvm)}")
    print(f"Arquivo: {args.saida} ({os.path.getsize(args.saida) // 1024} KB)")


if __name__ == "__main__":
    principal()
