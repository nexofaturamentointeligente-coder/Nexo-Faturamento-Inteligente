"""Golden de regressão do motor: Raio-X (raio_x.diagnosticar) e cálculo da análise
(etl_processor._calcular_kpis_local) sobre os conjuntos de teste históricos.

A referência (tests/fixtures/raio_x_regression_expected.json) foi capturada do motor em
91e5fb0, ANTES do saneamento de 11/2026 (filtro de rodapés, parser de quantidades, compras e
codificação). Todo valor capturado tem de continuar idêntico: o saneamento corrige os defeitos
nos relatórios que os têm (testes próprios em test_etl_saneamento.py) sem mexer no resultado de
relatório nenhum que não os tenha.

Campo NOVO só passa se estiver em ACRESCIMOS_APROVADOS, com o motivo. Nenhum valor antigo pode
mudar nem sumir; a única troca aprovada é o rótulo da versão das regras do Raio-X
(VALORES_ALTERADOS_APROVADOS), que subiu com o saneamento (11.4) e com a unificação do motor (11.5).
Nenhum conjunto histórico tem item de custo implausível nem total informado com fração de centavo
que mude a soma: as duas regras da unificação não alteram valor nenhum daqui (os testes delas estão
em test_cobertura_leiautes_erp.py e test_planos_raio_x.py).

Conjuntos:
  - os 22 pares sintéticos do golden SEC-005 (tests/etl_equivalence_support.fixture_pairs): a
    venda de cada par no Raio-X e o par na análise;
  - os relatórios de janeiro e fevereiro do Raio-X (tests/raio_x_support);
  - um lote de 6 meses no formato de um PDV real (cp1252, ";", título e período no topo,
    acentos, granel, preço com variação, devolução, cancelamento, conflito de código, custo
    ausente, venda abaixo do custo, estoque negativo e rodapé TOTAL GERAL), com as compras de
    cada mês, e o primeiro mês também em XLSX.

Recapturar (só com motivo documentado, numa cópia sem .env), informando o commit do motor que roda:
    python -B -m tests.test_raio_x_regression --capturar --commit <commit>
e depois trocar COMMIT_DE_REFERENCIA para o mesmo commit.
"""

from calendar import monthrange
from contextlib import ExitStack
import fnmatch
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest

from openpyxl import Workbook

from tests.etl_equivalence_support import fixture_pairs, plain, raw_snapshot
from tests.raio_x_support import FEVEREIRO, JANEIRO, executar_como_o_worker

EXPECTED = Path(__file__).parent / "fixtures" / "raio_x_regression_expected.json"
COMMIT_DE_REFERENCIA = "91e5fb0"

# Caminho (padrão fnmatch, com "." entre as chaves e [n] nos índices) -> motivo do acréscimo.
_CMV_LIQUIDO = ("Saneamento 11/2026, item 1.5: CMV e margem líquidos com bonificações e devoluções. "
                "Campo novo; custo_total e margem_valor seguem iguais.")
ACRESCIMOS_APROVADOS: dict = {
    "raio_x.*.resultado.auditoria.custo_bonificacoes": _CMV_LIQUIDO,
    "raio_x.*.resultado.auditoria.custo_devolucoes": _CMV_LIQUIDO,
    "raio_x.*.resultado.auditoria.cmv_liquido": _CMV_LIQUIDO,
    "raio_x.*.resultado.auditoria.margem_liquida_valor": _CMV_LIQUIDO,
    "raio_x.*.resultado.auditoria.margem_liquida_percentual": _CMV_LIQUIDO,
    "raio_x.*.resultado.auditoria.custo_a_confirmar": (
        "Unificação 11/2026: os itens de custo implausível (custo a partir de 3 vezes a venda), que saem da "
        "perda e do recuperável. Campo novo; nos conjuntos históricos vem zerado."),
}
# Caminho (padrão fnmatch) -> (valor da referência, valor aprovado, motivo). Só o rótulo da versão:
# nenhum valor de relatório pode mudar.
VALORES_ALTERADOS_APROVADOS: dict = {
    "raio_x.*.resultado.versao": ("raio-x-2026-11.3", "raio-x-2026-11.5",
                                  "Saneamento 11/2026 (11.4) e unificação do motor 11/2026 (11.5): a versão das "
                                  "regras do Raio-X sobe para separar o resultado gravado antes do gravado depois."),
}


# ---------------------------------------------------------------------------
# Lote sintético no formato de um PDV real
# ---------------------------------------------------------------------------
# (código, descrição, preço, custo unitário ou None, granel)
PRODUTOS = (
    ("101", "Café Pilão 500g", 18.90, 14.20, False),
    ("102", "Açúcar Cristal 1kg", 4.99, 3.80, False),
    ("103", "Feijão Carioca 1kg", 8.49, 6.90, False),
    ("104", "Arroz Branco 5kg", 27.90, 22.40, False),
    ("105", "Óleo de Soja 900ml", 7.99, 6.30, False),
    ("106", "Leite Integral 1L", 5.49, 4.95, False),
    ("107", "Pão Francês kg", 16.90, 9.10, True),
    ("108", "Maçã Fuji kg", 12.99, 8.40, True),
    ("109", "Biscoito Recheado 140g", 3.29, None, False),
    ("110", "Refrigerante Cola 2L", 9.99, 7.20, False),
    ("111", "Detergente Neutro 500ml", 2.79, None, False),
    ("112", "Sabão em Pó 1kg", 14.50, 15.80, False),
    ("113", "Papel Higiênico 12 rolos", 21.90, 16.30, False),
    ("114", "Macarrão Espaguete 500g", 4.49, 3.10, False),
    ("115", "Molho de Tomate 340g", 2.99, 2.15, False),
    ("116", "Queijo Mussarela kg", 49.90, 38.00, True),
    ("117", "Presunto Cozido kg", 39.90, 41.50, True),
    ("118", "Água Mineral 500ml", 1.99, 0.85, False),
    ("119", "Cerveja Lata 350ml", 3.99, 3.10, False),
    ("120", "Chocolate ao Leite 90g", 6.49, 4.20, False),
)


def _br(valor: float, casas: int = 2) -> str:
    """Número no formato do PDV brasileiro: milhar com ponto e decimal com vírgula."""
    inteiro, _, decimal = f"{abs(valor):,.{casas}f}".partition(".")
    texto = inteiro.replace(",", ".") + ("," + decimal if casas else "")
    return ("-" if valor < 0 else "") + texto


def _linhas_do_mes(mes: int) -> list:
    """Linhas de venda do mês: (código, descrição, qtde, preço, total, custo, estoque, situação)."""
    linhas = []
    for posicao, (codigo, nome, preco, custo, granel) in enumerate(PRODUTOS):
        if granel:
            quantidade = round(3.115 + posicao * 0.731 + mes * 0.457, 3)
        else:
            quantidade = float(4 + (posicao * 7 + mes * 3) % 23)
        # O custo do óleo sobe 3% ao mês sem repasse de preço (precificação desatualizada).
        custo_mes = None if custo is None else round(custo * (1.03 ** (mes - 1) if codigo == "105" else 1), 2)
        # Feijão e queijo com saldo negativo em alguns meses (inconsistência de estoque do PDV).
        estoque = (posicao + mes) % 5 - 3 if codigo in ("103", "116") else (posicao * 5 + mes) % 17
        linhas.append([codigo, nome, quantidade, preco, round(quantidade * preco, 2),
                       None if custo_mes is None else round(quantidade * custo_mes, 2), estoque, "Concluída"])
    # Preço com variação: uma venda do café com desconto nos meses pares.
    if mes % 2 == 0:
        linhas.append(["101", "Café Pilão 500g", 3.0, 16.90, 50.70, round(3 * 14.20, 2), 5, "Concluída"])
    # Devolução e cancelamento, como o PDV exporta na coluna de situação.
    if mes in (2, 5):
        linhas.append(["104", "Arroz Branco 5kg", 1.0, 27.90, 27.90, 22.40, 9, "Devolução"])
    if mes == 3:
        linhas.append(["110", "Refrigerante Cola 2L", 2.0, 9.99, 19.98, 14.40, 4, "Cancelada"])
        # Mesmo código, outra descrição: conflito de cadastro.
        linhas.append(["113", "Papel Higiênico 12 rolos Folha Dupla", 2.0, 23.90, 47.80, 34.00, 3, "Concluída"])
    return linhas


def _relatorio_csv(mes: int) -> bytes:
    fim = monthrange(2025, mes)[1]
    linhas = _linhas_do_mes(mes)
    corpo = [
        "MERCADO SINTÉTICO LTDA - Vendas por Produto",
        f"Período: 01/{mes:02d}/2025 a {fim:02d}/{mes:02d}/2025",
        "",
        "Código;Descrição do Produto;Qtde;Preço Unitário;Valor Total;Total Custo;Estoque;Situação",
    ]
    for codigo, nome, qtde, preco, total, custo, estoque, situacao in linhas:
        corpo.append(";".join([
            codigo, nome, _br(qtde, 3) if qtde != int(qtde) else str(int(qtde)), _br(preco), _br(total),
            "" if custo is None else _br(custo), str(estoque), situacao,
        ]))
    total_geral = round(sum(linha[4] for linha in linhas if linha[7] == "Concluída"), 2)
    corpo.append(f";TOTAL GERAL;;;{_br(total_geral)};;;")
    return ("\r\n".join(corpo) + "\r\n").encode("cp1252")


def _compras_csv(mes: int) -> bytes:
    if mes == 6:  # nível de nota fiscal: fornecedor e valor
        corpo = ["Fornecedor;Data NF;Valor NF",
                 "Distribuidora Sintética Ltda;05/06/2025;1.250,40",
                 "Atacado Fictício S/A;18/06/2025;3.418,95"]
    else:
        corpo = ["Código;Produto;Qtde Comprada;Total Custo"]
        for posicao, (codigo, nome, _, custo, granel) in enumerate(PRODUTOS):
            if posicao % 3 == mes % 3:
                continue
            quantidade = 12 + (posicao * 5 + mes) % 19
            corpo.append(f"{codigo};{nome};{quantidade};{_br(quantidade * (custo or 2.5))}")
    return ("\n".join(corpo) + "\n").encode("cp1252")


def _relatorio_xlsx(destino: Path, mes: int) -> Path:
    livro = Workbook()
    folha = livro.active
    folha.title = "Vendas"
    fim = monthrange(2025, mes)[1]
    folha.append(["MERCADO SINTÉTICO LTDA - Vendas por Produto"])
    folha.append([f"Período: 01/{mes:02d}/2025 a {fim:02d}/{mes:02d}/2025"])
    folha.append(["Código", "Descrição do Produto", "Qtde", "Preço Unitário", "Valor Total", "Total Custo",
                  "Estoque", "Situação"])
    for linha in _linhas_do_mes(mes):
        folha.append(linha)
    livro.save(destino)
    livro.close()
    return destino


def montar_conjuntos(diretorio: Path) -> dict:
    """Caminhos de todos os conjuntos, criados em `diretorio`."""
    diretorio = Path(diretorio)
    pares = fixture_pairs(diretorio / "pares")
    lote = diretorio / "lote"
    lote.mkdir(parents=True, exist_ok=True)
    meses = []
    for mes in range(1, 7):
        vendas = lote / f"vendas_2025_{mes:02d}.csv"
        compras = lote / f"compras_2025_{mes:02d}.csv"
        vendas.write_bytes(_relatorio_csv(mes))
        compras.write_bytes(_compras_csv(mes))
        meses.append((vendas, compras))
    raio = diretorio / "raio_x"
    raio.mkdir(parents=True, exist_ok=True)
    janeiro, fevereiro = raio / "janeiro.csv", raio / "fevereiro.csv"
    janeiro.write_bytes(JANEIRO)
    fevereiro.write_bytes(FEVEREIRO)
    compras_raio = raio / "compras.csv"
    compras_raio.write_bytes("Codigo;Produto;Qtde Comprada;Total Custo\n1;Cafe 500g;30;450,00\n"
                             "2;Acucar 1kg;20;80,00\n3;Leite 1L;50;210,00\n".encode("utf-8"))
    xlsx = _relatorio_xlsx(lote / "vendas_2025_01.xlsx", 1)
    return {"pares": pares, "meses": meses, "janeiro": janeiro, "fevereiro": fevereiro,
            "compras_raio": compras_raio, "xlsx": xlsx}


# ---------------------------------------------------------------------------
# Execução do motor (no próprio processo: o worker real só roda em Linux)
# ---------------------------------------------------------------------------
def _raio_x(caminhos) -> dict:
    from etl_inputs import ETLInputError
    try:
        return {"resultado": executar_como_o_worker([str(c) for c in caminhos])}
    except ETLInputError as erro:
        return {"erro": erro.code, "mensagem": erro.safe_message}


def _analise(vendas, compras) -> dict:
    from etl_inputs import ETLInputError
    from etl_limits import ETLLimits, using_limits
    import etl_processor
    try:
        with using_limits(ETLLimits()):
            resultado = etl_processor._calcular_kpis_local(str(vendas), str(compras))
    except ETLInputError as erro:
        return {"erro": erro.code, "mensagem": erro.safe_message}
    return {"resultado": json.loads(json.dumps(plain(resultado._asdict()), ensure_ascii=True, allow_nan=False))}


def _bruto(caminho) -> dict:
    import etl_processor
    from etl_limits import ETLLimits, using_limits
    with using_limits(ETLLimits()):
        return raw_snapshot(etl_processor._ler_bruto(str(caminho)))


def capturar(diretorio: Path) -> dict:
    conjuntos = montar_conjuntos(diretorio)
    saida = {"raio_x": {}, "analise": {}, "bruto": {}}
    for caso, (vendas, compras) in conjuntos["pares"].items():
        saida["raio_x"]["par_" + caso] = _raio_x([vendas])
        saida["analise"]["par_" + caso] = _analise(vendas, compras)
    saida["raio_x"]["janeiro_fevereiro"] = _raio_x([conjuntos["janeiro"], conjuntos["fevereiro"]])
    saida["raio_x"]["fevereiro_janeiro"] = _raio_x([conjuntos["fevereiro"], conjuntos["janeiro"]])
    saida["analise"]["janeiro"] = _analise(conjuntos["janeiro"], conjuntos["compras_raio"])
    saida["raio_x"]["lote_6_meses"] = _raio_x([vendas for vendas, _ in conjuntos["meses"]])
    saida["raio_x"]["lote_xlsx_janeiro"] = _raio_x([conjuntos["xlsx"]])
    for numero, (vendas, compras) in enumerate(conjuntos["meses"], 1):
        saida["raio_x"][f"lote_mes_{numero:02d}"] = _raio_x([vendas])
        saida["analise"][f"lote_mes_{numero:02d}"] = _analise(vendas, compras)
        saida["bruto"][f"lote_vendas_{numero:02d}"] = _bruto(vendas)
        saida["bruto"][f"lote_compras_{numero:02d}"] = _bruto(compras)
    saida["bruto"]["lote_xlsx_janeiro"] = _bruto(conjuntos["xlsx"])
    return saida


# ---------------------------------------------------------------------------
# Testes
# ---------------------------------------------------------------------------
class RegressaoDoMotorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.referencia = json.loads(EXPECTED.read_text(encoding="utf-8"))
        if cls.referencia.get("source_commit") != COMMIT_DE_REFERENCIA:
            raise AssertionError("Referência do golden de regressão incorreta.")
        pilha = ExitStack()
        cls.addClassCleanup(pilha.close)
        pasta = Path(pilha.enter_context(tempfile.TemporaryDirectory(prefix="nexo-regressao-")))
        cls.atual = capturar(pasta)

    def _aprovado(self, caminho: str) -> bool:
        return any(fnmatch.fnmatchcase(caminho, padrao) for padrao in ACRESCIMOS_APROVADOS)

    def _conferir(self, atual, esperado, caminho):
        if isinstance(esperado, dict):
            self.assertIsInstance(atual, dict, caminho)
            sumiram = sorted(set(esperado) - set(atual))
            self.assertFalse(sumiram, f"{caminho}: campos sumiram: {sumiram}")
            for chave in sorted(set(atual) - set(esperado)):
                self.assertTrue(self._aprovado(f"{caminho}.{chave}"),
                                f"{caminho}.{chave}: campo novo sem aprovação em ACRESCIMOS_APROVADOS")
            for chave, valor in esperado.items():
                self._conferir(atual[chave], valor, f"{caminho}.{chave}")
        elif isinstance(esperado, list):
            self.assertIsInstance(atual, list, caminho)
            self.assertEqual(len(atual), len(esperado), f"{caminho}: tamanho da lista mudou")
            for indice, valor in enumerate(esperado):
                self._conferir(atual[indice], valor, f"{caminho}[{indice}]")
        elif isinstance(esperado, float) and isinstance(atual, float) and math.isnan(esperado):
            self.assertTrue(math.isnan(atual), caminho)
        else:
            for padrao, (de, para, _motivo) in VALORES_ALTERADOS_APROVADOS.items():
                if fnmatch.fnmatchcase(caminho, padrao) and esperado == de:
                    self.assertEqual(atual, para, f"{caminho}: troca aprovada em VALORES_ALTERADOS_APROVADOS")
                    return
            # Idêntico, inclusive o tipo: 10 e 10.0 contam como mudança.
            self.assertEqual(type(atual), type(esperado), f"{caminho}: tipo mudou")
            self.assertEqual(atual, esperado, caminho)

    def _grupo(self, grupo):
        esperado = self.referencia[grupo]
        self.assertEqual(sorted(self.atual[grupo]), sorted(esperado), f"{grupo}: casos diferentes")
        for caso in esperado:
            with self.subTest(grupo=grupo, caso=caso):
                self._conferir(self.atual[grupo][caso], esperado[caso], f"{grupo}.{caso}")

    def test_raio_x_identico_nos_conjuntos_historicos(self):
        self._grupo("raio_x")

    def test_analise_identica_nos_conjuntos_historicos(self):
        self._grupo("analise")

    def test_leitura_bruta_identica_no_lote_sintetico(self):
        self._grupo("bruto")

    def test_custo_a_confirmar_vem_zerado_em_todos_os_conjuntos_historicos(self):
        # O campo novo é aprovado inteiro (ACRESCIMOS_APROVADOS): aqui se confere que ele não esconde mudança.
        vazio = {"itens": 0, "faturamento_com_custo": 0.0, "custo_total": 0.0, "perda_desconsiderada": 0.0, "lista": []}
        for caso, saida in self.atual["raio_x"].items():
            if "resultado" in saida:
                with self.subTest(caso=caso):
                    self.assertEqual(saida["resultado"]["auditoria"]["custo_a_confirmar"], vazio)

    def test_lote_sintetico_exercita_o_que_promete(self):
        # O golden só protege o que o lote tem: conferido na própria referência.
        lote = self.referencia["raio_x"]["lote_6_meses"]["resultado"]
        saude = lote["saude"]
        self.assertGreater(saude["itens_sem_custo"], 0)
        self.assertGreater(saude["inconsistencias"]["codigo_com_varios_nomes"], 0)
        self.assertGreater(saude["inconsistencias"]["estoque_negativo"], 0)
        self.assertGreater(saude["devolucoes"], 0)
        self.assertGreater(lote["auditoria"]["itens_venda_abaixo_custo"], 0)
        self.assertGreater(lote["scanner"]["itens_com_variacao"], 0)
        self.assertTrue(lote["auditoria"]["precificacao"])
        analise = self.referencia["analise"]["lote_mes_03"]["resultado"]
        self.assertGreater(analise["telemetria"]["linhas_canceladas"], 0)


if __name__ == "__main__":
    if "--capturar" in sys.argv:
        # A proveniência gravada é a do motor que rodou: o commit é obrigatório, e o golden só aceita a
        # referência depois que COMMIT_DE_REFERENCIA for trocado para ele.
        indice = sys.argv.index("--capturar")
        if len(sys.argv) <= indice + 2 or sys.argv[indice + 1] != "--commit" or not sys.argv[indice + 2]:
            sys.exit("Uso: python -B -m tests.test_raio_x_regression --capturar --commit <commit do motor>")
        commit = sys.argv[indice + 2]
        from etl_limits import ETLLimits, using_limits
        with tempfile.TemporaryDirectory(prefix="nexo-regressao-") as pasta, using_limits(ETLLimits()):
            capturado = capturar(Path(pasta))
        EXPECTED.write_text(json.dumps({"source_commit": commit, **capturado}, ensure_ascii=False,
                                       indent=1, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
        print("Referência gravada em", EXPECTED, "com o commit", commit)
        if commit != COMMIT_DE_REFERENCIA:
            print("Troque COMMIT_DE_REFERENCIA para", commit, "e registre o motivo da recaptura.")
    else:
        unittest.main()
