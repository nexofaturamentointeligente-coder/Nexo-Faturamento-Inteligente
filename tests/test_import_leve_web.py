"""O processo web não carrega pandas, numpy nem openpyxl.

As três bibliotecas só servem ao motor, que roda no processo isolado do ETL (etl_worker). No processo web
elas ocupavam cerca de 40 MB de memória residente pela vida inteira do processo, porque o app importava o
motor só para ler constantes, exceções e funções puras. Esses nomes ficam nos módulos leves (etl_erros,
etl_regras, raio_x_regras, indicadores_regras e avaliacao_regras), só com a biblioteca padrão.

O teste sobe o app num Python limpo (-I) e roda os quatro executores do ETL com o processo isolado
simulado: etl_runtime._run_job devolve o que o worker devolveria, calculado aqui pelo motor de verdade.
Assim passam também as conferências que o processo pai faz depois do filho (as que importavam o motor).
"""

from datetime import date
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests.deploy_support import ROOT, ensure_test_environment
from tests.etl_equivalence_support import PURCHASES, SALES, write_csv
from tests.raio_x_support import FEVEREIRO, JANEIRO, avaliar_como_o_worker, executar_como_o_worker

_ENSAIO = r'''
import json
import sys

raiz, respostas_json = sys.argv[1], sys.argv[2]
sys.path.insert(0, raiz)
antes = set(sys.modules)
import avaliacao_regras, etl_erros, etl_regras, indicadores_regras, raio_x_regras, security_gatekeeper
proprios = {"avaliacao_regras", "etl_erros", "etl_limits", "etl_regras", "indicadores_regras", "raio_x_regras",
            "security_gatekeeper"}
fora_da_biblioteca_padrao = sorted({nome.split(".")[0] for nome in set(sys.modules) - antes}
                                   - set(sys.stdlib_module_names) - proprios)

from app import create_app
app = create_app("development")
pesados = ("pandas", "numpy", "openpyxl")
na_subida = sorted(nome for nome in pesados if nome in sys.modules)

import etl_runtime
import validacao_pdv
with open(respostas_json, encoding="utf-8") as arquivo:
    respostas = json.load(arquivo)
chamadas = []


def processo_isolado_simulado(operation, paths, *, extension=None, limits=None):
    chamadas.append(operation)
    return json.loads(json.dumps(respostas[operation]))


etl_runtime._run_job = processo_isolado_simulado
raio = etl_runtime.run_raio_x(["janeiro.csv", "fevereiro.csv"])
avaliacao = etl_runtime.run_avaliacao_cenario(["janeiro.csv", "fevereiro.csv"])
calculo = etl_regras.calcular_kpis_em_memoria("vendas.csv", "compras.csv")
triagem = etl_runtime.run_validation("vendas.csv", ".csv")
conferido = etl_regras.conferir_total_pdv(calculo, validacao_pdv.interpretar_valor_digitado("300,00"))
print(json.dumps({
    "fora_da_biblioteca_padrao": fora_da_biblioteca_padrao, "na_subida": na_subida,
    "no_fim": sorted(nome for nome in pesados if nome in sys.modules), "chamadas": chamadas,
    "versao_raio_x": raio["versao"], "versao_avaliacao": avaliacao["versao"],
    "plano": raio_x_regras.recomendar(raio, 12)["plano"], "faturamento": calculo.kpis["faturamento_total"],
    "conferencia": conferido.telemetria["conferencia_pdv"]["status"], "triagem": triagem["liberado"],
}))
'''


class ImportLeveDoWebTests(unittest.TestCase):
    def setUp(self):
        ensure_test_environment()
        self.pasta = Path(tempfile.mkdtemp(prefix="nexo-import-leve-"))
        self.addCleanup(shutil.rmtree, self.pasta, ignore_errors=True)

    def _respostas_do_worker(self):
        """O que o processo isolado devolveria em cada operação, calculado pelo motor neste processo."""
        from etl_limits import get_limits
        import etl_processor
        from security_gatekeeper import triar

        janeiro, fevereiro = self.pasta / "janeiro.csv", self.pasta / "fevereiro.csv"
        janeiro.write_bytes(JANEIRO)
        fevereiro.write_bytes(FEVEREIRO)
        meses = [str(janeiro), str(fevereiro)]
        vendas = write_csv(self.pasta / "vendas.csv", SALES)
        compras = write_csv(self.pasta / "compras.csv", PURCHASES)
        calculo = etl_processor._calcular_kpis_local(str(vendas), str(compras))._asdict()
        return {
            "raio_x": executar_como_o_worker(meses),
            "avaliacao_cenario": avaliar_como_o_worker(meses, hoje=date(2026, 11, 18)),
            # O worker grava as datas em ISO (etl_worker.encode).
            "calculate": json.loads(json.dumps(calculo, allow_nan=False, default=lambda valor: valor.isoformat())),
            "validate": triar(vendas, ".csv", get_limits()),
        }

    def test_o_app_e_os_executores_do_etl_nao_carregam_pandas_numpy_nem_openpyxl(self):
        import avaliacao_motor
        import raio_x

        respostas = self._respostas_do_worker()
        arquivo = self.pasta / "respostas.json"
        arquivo.write_text(json.dumps(respostas), encoding="utf-8")
        feito = subprocess.run([sys.executable, "-I", "-B", "-X", "utf8", "-c", _ENSAIO, str(ROOT), str(arquivo)],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
                               cwd=str(self.pasta), env=dict(os.environ), stdin=subprocess.DEVNULL, check=False)
        self.assertEqual(feito.returncode, 0, feito.stderr[-3000:])
        relatorio = json.loads(feito.stdout.strip().splitlines()[-1])
        self.assertEqual(relatorio["fora_da_biblioteca_padrao"], [], "os módulos leves só usam a biblioteca padrão")
        self.assertEqual(relatorio["na_subida"], [])
        self.assertEqual(relatorio["no_fim"], [])
        self.assertEqual(relatorio["chamadas"], ["raio_x", "avaliacao_cenario", "calculate", "validate"])
        self.assertEqual(relatorio["versao_raio_x"], raio_x.VERSAO_REGRAS)
        self.assertEqual(relatorio["versao_avaliacao"], avaliacao_motor.VERSAO_REGRAS_LEAD)
        self.assertEqual(relatorio["plano"], raio_x.recomendar(respostas["raio_x"], 12)["plano"])
        self.assertEqual(relatorio["faturamento"], respostas["calculate"]["kpis"]["faturamento_total"])
        self.assertEqual(relatorio["conferencia"], "CONCLUIDO")
        self.assertEqual(relatorio["triagem"], "original")

    def test_o_motor_reexporta_os_mesmos_objetos_dos_modulos_leves(self):
        """Uma só fonte: o motor e o processo web enxergam a mesma classe, constante e função."""
        import avaliacao_motor
        import avaliacao_regras
        import etl_erros
        import etl_inputs
        import etl_processor
        import etl_regras
        import indicadores_executivos
        import indicadores_regras
        import raio_x
        import raio_x_regras

        self.assertIs(etl_inputs.ETLInputError, etl_erros.ETLInputError)
        self.assertIs(etl_processor.ETLValidationError, etl_erros.ETLValidationError)
        self.assertTrue(issubclass(etl_processor.ETLLeiauteAmbiguo, etl_erros.ETLInputError))
        for nome in ("ResultadoETL", "LIMITE_CATEGORIA", "STATUS_PDV_DIVERGENTE", "converter_valor_monetario",
                     "em_centavos", "conferir_total_pdv", "processar_arquivos_analise", "calcular_kpis_em_memoria"):
            self.assertIs(getattr(etl_processor, nome), getattr(etl_regras, nome), nome)
        for nome in ("VERSAO_REGRAS", "MAX_ARQUIVOS", "MAX_FATIAS", "RAZAO_CUSTO_IMPLAUSIVEL", "CHAVES_RESULTADO",
                     "recomendar"):
            self.assertIs(getattr(raio_x, nome), getattr(raio_x_regras, nome), nome)
        for nome in ("VERSAO", "CHAVES", "STATUS", "MARGEM_MINIMA", "valido"):
            self.assertIs(getattr(indicadores_executivos, nome), getattr(indicadores_regras, nome), nome)
        for nome in ("VERSAO_REGRAS_LEAD", "COLUNAS", "TETOS", "CHAVES_META", "LIMITE_DESCRICAO"):
            self.assertIs(getattr(avaliacao_motor, nome), getattr(avaliacao_regras, nome), nome)


if __name__ == "__main__":
    unittest.main()
