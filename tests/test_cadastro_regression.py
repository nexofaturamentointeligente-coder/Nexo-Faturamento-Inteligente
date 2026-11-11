"""Golden complementar da Etapa 2: o Raio-X do cliente e a análise mensal não mudam com relatórios
que trazem NCM, categoria (grupo, seção, departamento, família), bonificação, devolução,
cancelamento e cabeçalhos ambíguos ("Cód. NCM" antes de "Código", "Grupo do Produto" antes da
descrição).

A Etapa 2 passou a ler NCM, categoria e os ajustes de bonificação e devolução por produto, só
para a Avaliação de Cenário (avaliacao_motor), atrás de um parâmetro desligado por padrão em
etl_processor._vendas_do_bruto e raio_x._ler_arquivo. O golden de regressão do 91e5fb0
(tests/test_raio_x_regression.py) não tem nenhum desses relatórios; este conjunto foi capturado
do motor em 53020e6, ANTES da Etapa 2, e todo valor tem de continuar idêntico, inclusive o tipo.

Além disso, nos mesmos relatórios, o Raio-X que a Avaliação de Cenário devolve junto da matriz é
o mesmo de raio_x.diagnosticar.

Mudanças aprovadas depois da captura (MUDANCAS_APROVADAS; todo o resto continua idêntico): a unificação
do motor de 11/2026 subiu a versão das regras do Raio-X para 11.5 e acrescentou à auditoria o bloco
"custo_a_confirmar" (itens de custo implausível, fora da perda e do recuperável). Nenhum relatório deste
conjunto tem item assim: o bloco vem zerado, e o teste confere isso antes de comparar o resto. Os
indicadores executivos de 11/2026 (11.6) acrescentaram o nó "indicadores_executivos", conferido aqui pelo
contrato (indicadores_executivos.valido) antes de comparar o resto; os valores dele têm testes próprios em
tests/test_indicadores.py. Nenhum relatório deste conjunto traz a data de cada venda: o Raio-X do cliente,
que agora fatia por mês a venda item a item, continua com um relatório por arquivo.

Recapturar (só com motivo documentado, numa cópia sem .env), informando o commit do motor que roda:
    python -B -m tests.test_cadastro_regression --capturar --commit <commit>
e depois trocar COMMIT_DE_REFERENCIA para o mesmo commit.
"""

from contextlib import ExitStack
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

from openpyxl import Workbook

import indicadores_executivos
from tests.test_raio_x_regression import _analise, _raio_x

EXPECTED = Path(__file__).parent / "fixtures" / "cadastro_regression_expected.json"
COMMIT_DE_REFERENCIA = "53020e6"

# O que mudou de propósito no Raio-X depois da captura (ver o texto do módulo).
MUDANCAS_APROVADAS = {
    "versao": ("raio-x-2026-11.4", "raio-x-2026-11.6"),
    "custo_a_confirmar": {"itens": 0, "faturamento_com_custo": 0.0, "custo_total": 0.0,
                          "perda_desconsiderada": 0.0, "lista": []},
}

_COMPRAS = "Fornecedor;Valor NF\nDistribuidora Alfa;1.000,00\n"


def _texto(periodo: str, cabecalho: str, linhas: list) -> str:
    return f"Relatório de Vendas por Produto\nPeríodo: {periodo}\n{cabecalho}\n" + "".join(l + "\n" for l in linhas)


# Março: NCM em texto (com zero à esquerda, com pontos, vazio e só zeros), grupo (com marcador
# genérico e vazio), situação (devolução e cancelamento), bonificação ("0,00" com custo) e estoque.
_MARCO = _texto(
    "01/03/2026 a 31/03/2026",
    "Código;Produto;NCM;Grupo;Situação;Qtde;Valor Total;Total Custo;Estoque",
    [
        "101;Leite Integral 1L;04012010;LATICINIOS;Concluída;30;150,00;120,00;12",
        "102;Café Torrado 500g;0901.21.00;MERCEARIA;Concluída;10;189,00;140,00;4",
        "103;Água Mineral 500ml;;BEBIDAS;Concluída;48;96,00;52,80;30",
        "104;Sabão em Pó 1kg;00000000;SEM GRUPO;Concluída;6;71,40;80,00;-2",
        "105;Biscoito Recheado;19053100;;Concluída;20;50,00;;8",
        "101;Leite Integral 1L;04012010;LATICINIOS;Concluída;2;0,00;8,00;12",
        "102;Café Torrado 500g;0901.21.00;MERCEARIA;Devolução;1;18,90;14,00;4",
        "103;Água Mineral 500ml;;BEBIDAS;Cancelada;5;10,00;5,50;30",
    ],
)
# Abril: Seção e Departamento no lugar de Grupo, um produto só com bonificação e a devolução de um
# produto vendido só em março.
_ABRIL = _texto(
    "01/04/2026 a 30/04/2026",
    "Código;Produto;NCM;Departamento;Seção;Situação;Qtde;Valor Total;Total Custo",
    [
        "101;Leite Integral 1L;04012010;ALIMENTOS;LATICINIOS;Concluída;25;125,00;100,00",
        "103;Água Mineral 500ml;22011000;BEBIDAS;AGUAS;Concluída;40;80,00;44,00",
        "106;Chocolate 90g;18063210;ALIMENTOS;DOCES;Concluída;0;0,00;3,00",
        "106;Chocolate 90g;18063210;ALIMENTOS;DOCES;Concluída;3;0,00;9,00",
        "104;Sabão em Pó 1kg;34022000;LIMPEZA;LAVANDERIA;Devolução;1;11,90;13,30",
        "107;Detergente 500ml;34022000;LIMPEZA;LAVANDERIA;Concluída;12;30,00;21,60",
    ],
)
# Cabeçalhos ambíguos: o dicionário de vendas reivindica "Cód. NCM" como código do produto e
# "Grupo do Produto" como nome do produto.
_COD_NCM_ANTES = _texto(
    "01/05/2026 a 31/05/2026",
    "Cód. NCM;Código;Descrição;Qtde;Valor Total;Total Custo",
    [
        "04012010;101;Leite Integral 1L;10;50,00;40,00",
        "04012010;108;Leite Desnatado 1L;8;44,00;33,60",
        "09012100;102;Café Torrado 500g;4;75,60;56,00",
    ],
)
_GRUPO_ANTES = _texto(
    "01/05/2026 a 31/05/2026",
    "Grupo do Produto;Descrição;Categoria;Qtde;Valor Total",
    [
        "LATICINIOS;Leite Integral 1L;ALIMENTOS;10;50,00",
        "LATICINIOS;Leite Desnatado 1L;ALIMENTOS;8;44,00",
        "MERCEARIA;Café Torrado 500g;ALIMENTOS;4;75,60",
    ],
)


def _xlsx(destino: Path) -> Path:
    """Março em XLSX, com o NCM em célula numérica (o zero à esquerda some) e a Família."""
    livro = Workbook()
    folha = livro.active
    folha.append(["Relatório de Vendas por Produto"])
    folha.append(["Período: 01/03/2026 a 31/03/2026"])
    folha.append(["Código", "Produto", "NCM", "Família", "Qtde", "Valor Total", "Total Custo"])
    folha.append(["101", "Leite Integral 1L", 4012010, "LATICINIOS", 30, 150.0, 120.0])
    folha.append(["102", "Café Torrado 500g", 9012100, "MERCEARIA", 10, 189.0, 140.0])
    folha.append(["103", "Água Mineral 500ml", None, "BEBIDAS", 48, 96.0, 52.8])
    folha.append(["104", "Sabão em Pó 1kg", 0, None, 6, 71.4, 80.0])
    livro.save(destino)
    return destino


def montar_conjuntos(diretorio: Path) -> dict:
    def gravar(nome, texto, encoding="utf-8"):
        caminho = diretorio / nome
        caminho.write_bytes(texto.encode(encoding))
        return caminho
    return {
        "marco": gravar("marco.csv", _MARCO),
        "abril": gravar("abril.csv", _ABRIL),
        "marco_cp1252": gravar("marco_cp1252.csv", _MARCO, "cp1252"),
        "cod_ncm_antes": gravar("cod_ncm_antes.csv", _COD_NCM_ANTES),
        "grupo_antes": gravar("grupo_antes.csv", _GRUPO_ANTES),
        "xlsx": _xlsx(diretorio / "marco.xlsx"),
        "compras": gravar("compras.csv", _COMPRAS),
    }


def capturar(diretorio: Path) -> dict:
    c = montar_conjuntos(diretorio)
    saida = {"raio_x": {}, "analise": {}}
    for caso in ("marco", "abril", "marco_cp1252", "cod_ncm_antes", "grupo_antes", "xlsx"):
        saida["raio_x"][caso] = _raio_x([c[caso]])
        saida["analise"][caso] = _analise(c[caso], c["compras"])
    saida["raio_x"]["marco_abril"] = _raio_x([c["marco"], c["abril"]])
    saida["raio_x"]["abril_marco_xlsx"] = _raio_x([c["abril"], c["xlsx"]])
    return saida


class CadastroRegressaoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.referencia = json.loads(EXPECTED.read_text(encoding="utf-8"))
        if cls.referencia.get("source_commit") != COMMIT_DE_REFERENCIA:
            raise AssertionError("Referência do golden complementar incorreta.")
        pilha = ExitStack()
        cls.addClassCleanup(pilha.close)
        cls.pasta = Path(pilha.enter_context(tempfile.TemporaryDirectory(prefix="nexo-cadastro-")))
        cls.atual = capturar(cls.pasta)

    def _sem_as_mudancas_aprovadas(self, saida):
        """A saída atual do Raio-X como a referência a gravou: confere e desfaz só as mudanças aprovadas."""
        saida = copy.deepcopy(saida)
        resultado = saida.get("resultado")
        if resultado is not None:
            antiga, nova = MUDANCAS_APROVADAS["versao"]
            self.assertEqual(resultado["versao"], nova)
            resultado["versao"] = antiga
            self.assertEqual(resultado["auditoria"].pop("custo_a_confirmar"), MUDANCAS_APROVADAS["custo_a_confirmar"])
            self.assertTrue(indicadores_executivos.valido(resultado.pop("indicadores_executivos")))
        return saida

    def _grupo(self, grupo):
        esperado = self.referencia[grupo]
        self.assertEqual(sorted(self.atual[grupo]), sorted(esperado), f"{grupo}: casos diferentes")
        for caso, valor in esperado.items():
            with self.subTest(grupo=grupo, caso=caso):
                atual = self.atual[grupo][caso]
                if grupo == "raio_x":
                    atual = self._sem_as_mudancas_aprovadas(atual)
                # Idêntico, inclusive o tipo (o JSON gravado separa 10 de 10.0).
                self.assertEqual(json.dumps(atual, sort_keys=True),
                                 json.dumps(valor, sort_keys=True), f"{grupo}.{caso}")

    def test_raio_x_do_cliente_identico(self):
        self._grupo("raio_x")

    def test_analise_mensal_identica(self):
        self._grupo("analise")

    def test_o_conjunto_exercita_o_que_promete(self):
        marco = self.referencia["raio_x"]["marco"]["resultado"]
        self.assertGreater(marco["saude"]["devolucoes"], 0)
        self.assertGreater(marco["auditoria"]["custo_bonificacoes"], 0)
        self.assertGreater(marco["saude"]["inconsistencias"]["estoque_negativo"], 0)
        self.assertGreater(self.referencia["raio_x"]["marco_abril"]["resultado"]["auditoria"]["custo_devolucoes"], 0)
        self.assertIn("resultado", self.referencia["raio_x"]["cod_ncm_antes"])
        self.assertIn("resultado", self.referencia["raio_x"]["xlsx"])


if __name__ == "__main__":
    if "--capturar" in sys.argv:
        indice = sys.argv.index("--capturar")
        if len(sys.argv) <= indice + 2 or sys.argv[indice + 1] != "--commit" or not sys.argv[indice + 2]:
            sys.exit("Uso: python -B -m tests.test_cadastro_regression --capturar --commit <commit do motor>")
        commit = sys.argv[indice + 2]
        from etl_limits import ETLLimits, using_limits
        with tempfile.TemporaryDirectory(prefix="nexo-cadastro-") as pasta, using_limits(ETLLimits()):
            capturado = capturar(Path(pasta))
        EXPECTED.write_text(json.dumps({"source_commit": commit, **capturado}, ensure_ascii=False,
                                       indent=1, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
        print("Referência gravada em", EXPECTED, "com o commit", commit)
        if commit != COMMIT_DE_REFERENCIA:
            print("Troque COMMIT_DE_REFERENCIA para", commit, "e registre o motivo da recaptura.")
    else:
        unittest.main()
