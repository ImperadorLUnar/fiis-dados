# Dados de FIIs da B3

`fiis.json` com os FIIs negociados na B3, gerado todo dia útil depois do fechamento por `gerar_fiis.py`.

Fontes públicas: B3 (séries históricas COTAHIST) e CVM Dados Abertos (informe mensal de FII).
Só entram fundos com preço, P/VP e dividend yield calculados. Informação para estudo, não é recomendação de investimento.

Rodar localmente: `python gerar_fiis.py`
