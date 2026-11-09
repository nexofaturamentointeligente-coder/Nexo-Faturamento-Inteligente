"""Avaliação de Cenário, Etapa 2: contrato do processo isolado, gravação, rotas, comandos e downgrade.

  1. Contrato (etl_runtime.validar_avaliacao): a partir de um resultado válido do motor real (março e
     abril de 2026), um campo trocado de cada vez é recusado com ETLInputError("worker"), nunca com outra
     exceção; o vizinho aceito de cada fronteira mostra que a recusa é daquela regra. Também o teto do
     dinheiro da meta no próprio motor (avaliacao_motor.conferir_tetos_da_meta).
  2. Gravação no SQLite das migrações reais (avaliacao_cenario): a ida e volta da staging (cada coluna do
     motor relida igual à quantizada, os blocos do diagnóstico iguais ao resumo gravado, a retriagem
     reproduzindo a triagem gravada), o cabeçalho ambíguo sem omissão no ERP, a retenção de 30 dias na
     retriagem, limiares de outra versão ou ilegíveis, o teto do vazamento total do lead e o texto que o
     banco recusa na gravação chamada direto.
  3. Rotas e comandos (blueprints/leads.py e `flask leads retriar`): retriagem vencida recusada em todos
     os caminhos, aviso dos limiares na tela, log da leitura do JSON só com identificadores, limiares
     gravados com NaN, lista sem o esquema e resumo ou meta gravados fora do formato.
  4. Downgrade da 7c4e2a9b5d31: recusado enquanto um lead (mesmo expirado e sem linhas) guarda as colunas
     da Etapa 2.

O motor roda no próprio processo (raio_x_support.avaliar_como_o_worker), com a data fixa de 08/11/2026.
Os dados são fictícios.
"""

import contextlib
from datetime import date, timedelta
from decimal import Decimal
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from markupsafe import escape
from sqlalchemy import inspect, select, text

import avaliacao_motor
from etl_inputs import ETLInputError
import etl_runtime
import triagem_cenario
from tests.deploy_support import ROOT
from tests.raio_x_support import avaliar_como_o_worker
from tests.test_avaliacao_cenario import (CONTATO, DEZ25, FEV25, JAN25, LOTE, LOTE_MESMOS_MESES, NOV25, _Arquivos,
                                          _BancoCase, _linhas)
from tests.test_cadastro_regression import montar_conjuntos

HOJE = date(2026, 11, 8)
PAGINA = "/admin/leads"
REVISAO_ETAPA_2 = "7c4e2a9b5d31"
REVISAO_ANTERIOR = "6a8f2d4c0e13"
# Um resumo de bloco válido que a triagem real não gravaria (os lotes daqui têm no máximo 2 itens por bloco).
RESUMO_SENTINELA = json.dumps({"versao": triagem_cenario.VERSAO_TRIAGEM, "itens": 7, "faturamento": "1.00",
                               "recuperavel": "0.00", "motivos": {}})


def _cadastro(pasta, *casos):
    """O motor real sobre os relatórios de cadastro do golden da Etapa 2 (tests/test_cadastro_regression)."""
    conjuntos = montar_conjuntos(pasta)
    return avaliar_como_o_worker([str(conjuntos[caso]) for caso in casos], hoje=HOJE)


def _sem_constante(nome):
    raise AssertionError(f"{nome} no JSON")


def _limiares_de_outra_versao() -> str:
    """Limiares gravados pela triagem 11.2 (as mesmas chaves), com valores diferentes dos padrão atuais."""
    dados = triagem_cenario.Limiares(margem_baixa=Decimal("0.05"), queda_critica=Decimal("0.40")).para_dict()
    dados["versao"] = "triagem-2026-11.2"
    return json.dumps(dados)


# =============================================================================
# 1. Contrato do processo isolado
# =============================================================================
class ContratoDaEtapa2Tests(_Arquivos):
    """Um campo do resultado válido trocado de cada vez: recusa com o código "worker", nunca outra
    exceção (OverflowError, UnicodeError ou erro 500 na gravação)."""

    def test_json_aninhado_demais_do_processo_isolado_vira_recusa(self):
        # O json.loads do _run_job levanta RecursionError com 100 mil níveis: a recusa precisa ser a do
        # contrato (ETLInputError "worker"), e não uma exceção que viraria erro 500 na rota.
        import etl_runtime
        from etl_limits import ETLLimits

        class Processo:
            returncode = 0

            def __init__(self, args, **_kwargs):
                Path(args[-1]).write_text("[" * 100_000 + "]" * 100_000)

            def wait(self, timeout=None):
                return 0

            def poll(self):
                return 0

        with patch("etl_runtime.subprocess.Popen", Processo), self.assertRaises(ETLInputError) as erro:
            etl_runtime.run_avaliacao_cenario(["x.csv"], ETLLimits())
        self.assertEqual(erro.exception.code, "worker")

    def setUp(self):
        super().setUp()
        self.valido = _cadastro(self.pasta, "marco", "abril")
        self.posicao = {nome: i for i, nome in enumerate(avaliacao_motor.COLUNAS)}
        nomes = [valores[self.posicao["descricao_produto"]] for valores in self.valido["linhas"]["valores"]]
        self.leite, self.detergente = nomes.index("Leite Integral 1L"), nomes.index("Detergente 500ml")

    def _copia(self, mudar):
        copia = json.loads(json.dumps(self.valido))
        mudar(copia)
        return copia

    def _linha(self, indice, **valores):
        def mudar(resultado):
            for nome, valor in valores.items():
                resultado["linhas"]["valores"][indice][self.posicao[nome]] = valor
        return mudar

    def _conferir(self, *, aceitos: dict, recusados: dict):
        for caso, mudar in aceitos.items():
            with self.subTest(aceito=caso):
                copia = self._copia(mudar)
                self.assertIs(etl_runtime.validar_avaliacao(copia), copia)
        for caso, mudar in recusados.items():
            with self.subTest(recusado=caso):
                with self.assertRaises(ETLInputError) as erro:
                    etl_runtime.validar_avaliacao(self._copia(mudar))
                self.assertEqual(erro.exception.code, "worker")

    def test_contagens_de_relatorios_e_de_linhas_fora_da_faixa(self):
        # O Leite vendeu nos 2 relatórios de 2026, com custo, em 2 linhas; o Detergente, só em abril.
        valores = self.valido["linhas"]["valores"]
        campos = ("linhas", "meses", "meses_com_custo", "meses_completos", "relatorios_desde_primeira_venda")
        self.assertEqual([valores[self.leite][self.posicao[c]] for c in campos], [2, 2, 2, 2, 2])
        self.assertEqual(valores[self.detergente][self.posicao["relatorios_desde_primeira_venda"]], 1)
        self.assertEqual(self.valido["meta"]["arquivos_por_ano"], {"2026": 2})
        leite, detergente = self.leite, self.detergente
        self._conferir(
            aceitos={
                "o resultado do motor": lambda r: None,
                "linhas no teto do INTEGER": self._linha(leite, linhas=2 ** 31 - 1),
                # Só a venda em 1 relatório, sem custo nem relatório completo: o mínimo de "meses" é 1.
                "venda em 1 relatório": self._linha(leite, meses=1, meses_com_custo=0, meses_completos=0),
                "base do giro com todos os relatórios do ano": self._linha(detergente, relatorios_desde_primeira_venda=2),
            },
            recusados={
                "relatórios com venda acima dos do ano":
                    self._linha(leite, linhas=3, meses=3, relatorios_desde_primeira_venda=3),
                "linhas acima do INTEGER": self._linha(leite, linhas=2 ** 31),
                "menos linhas que relatórios com venda": self._linha(leite, linhas=1),
                "nenhum relatório com venda": self._linha(leite, meses=0, meses_com_custo=0, meses_completos=0),
                "base do giro abaixo dos relatórios com venda": self._linha(leite, relatorios_desde_primeira_venda=1),
                "base do giro acima dos relatórios do ano": self._linha(detergente, relatorios_desde_primeira_venda=3),
            },
        )

    def test_texto_com_caractere_nao_imprimivel_e_recusado(self):
        self.assertEqual(self.valido["linhas"]["categorias"][0], "BEBIDAS")
        leite = self.leite

        def categoria(nome):
            return lambda r: r["linhas"]["categorias"].__setitem__(0, nome)
        self._conferir(
            aceitos={
                "categoria com acento": categoria("BEBIDAS E ÁGUAS"),
                "descrição com acento e travessão": self._linha(leite, descricao_produto="Leite Integral 1L – Caixa"),
                "código com letras": self._linha(leite, codigo_produto="LT-101"),
                "sem código": self._linha(leite, codigo_produto=None),
            },
            recusados={
                "categoria com NUL": categoria("BEBI\x00DAS"),
                "categoria com inversão de direção (U+202E)": categoria("BEBI\u202eDAS"),
                "descrição com NUL": self._linha(leite, descricao_produto="Leite\x00 Integral 1L"),
                "descrição com surrogate solto": self._linha(leite, descricao_produto="Leite Integral \ud800"),
                "código com NUL": self._linha(leite, codigo_produto="10\x001"),
                "código com surrogate solto": self._linha(leite, codigo_produto="101\ud800"),
            },
        )

    def test_categoria_num_ano_sem_a_coluna_de_categoria(self):
        # Sem nenhuma "categoria ausente", o que sobra para a regra é o índice de categoria das linhas.
        def sem_ausentes(resultado):
            for valores in resultado["linhas"]["valores"]:
                valores[self.posicao["categoria_ausente"]] = False

        def sem_a_coluna(resultado):
            sem_ausentes(resultado)
            resultado["meta"]["relatorios_com_categoria_por_ano"]["2026"] = 0
        self.assertTrue(any(v[self.posicao["categoria"]] is not None for v in self.valido["linhas"]["valores"]))
        self._conferir(aceitos={"categoria num ano com a coluna": sem_ausentes},
                       recusados={"categoria num ano sem a coluna": sem_a_coluna})

    def test_inteiro_enorme_e_recusado_sem_overflow(self):
        # O json.loads aceita inteiros de até 4.300 dígitos; math.isfinite(10 ** 400) levantaria OverflowError.
        self.assertFalse(etl_runtime._numero(10 ** 400))
        self.assertTrue(etl_runtime._numero(2 ** 53))
        self.assertFalse(etl_runtime._numero(2 ** 53 + 1))
        self._conferir(aceitos={}, recusados={
            "venda bruta com 401 dígitos": self._linha(self.leite, valor_venda_bruto=10 ** 400),
            "estoque com 401 dígitos": self._linha(self.leite, estoque_final=-(10 ** 400)),
            "faturamento do Raio-X com 401 dígitos":
                lambda r: r["raio_x"]["saude"].__setitem__("faturamento_total", 10 ** 400),
            "valor cancelado com 401 dígitos": lambda r: r["meta"]["situacao"].__setitem__("valor_cancelado", 10 ** 400),
        })

    def test_periodo_invertido_e_saude_fora_do_formato(self):
        def periodo(inicio, fim):
            return lambda r: r["raio_x"]["periodo"].update(inicio=inicio, fim=fim)
        self._conferir(
            aceitos={"período de um dia": periodo("2026-04-30", "2026-04-30")},
            recusados={"início depois do fim": periodo("2026-05-01", "2026-04-30"),
                       "saúde trocada por uma lista": lambda r: r["raio_x"].__setitem__("saude", [r["raio_x"]["saude"]])},
        )

    def test_dinheiro_da_meta_tem_o_teto_do_dinheiro_das_linhas(self):
        meta = self.valido["meta"]
        self.assertEqual((meta["situacao"]["valor_cancelado"], meta["ajustes_sem_venda_por_ano"]["2026"]["custo_bonificacoes"]),
                         (10.0, 9))

        def ajuste(valor):
            return lambda r: r["meta"]["ajustes_sem_venda_por_ano"]["2026"].__setitem__("custo_bonificacoes", valor)

        def cancelado(valor):
            return lambda r: r["meta"]["situacao"].__setitem__("valor_cancelado", valor)
        self._conferir(
            aceitos={"ajuste sem venda logo abaixo do teto": ajuste(10 ** 12 - 1),
                     "ajuste sem venda um centavo abaixo do teto": ajuste(999999999999.99),
                     "valor cancelado logo abaixo do teto": cancelado(10 ** 12 - 1)},
            recusados={"ajuste sem venda no teto": ajuste(10 ** 12), "ajuste sem venda no teto, em float": ajuste(1e12),
                       "valor cancelado no teto": cancelado(10 ** 12),
                       "valor cancelado no teto, em float": cancelado(1e12)},
        )
        # O motor recusa antes de devolver, com a mensagem do teto (e não "resultado inválido").
        for nome, mudar in (("ajuste sem venda", ajuste), ("valor cancelado", cancelado)):
            with self.subTest(motor=nome):
                self.assertIsNone(avaliacao_motor.conferir_tetos_da_meta(self._copia(mudar(10 ** 12 - 1))["meta"]))
                with self.assertRaises(ETLInputError) as erro:
                    avaliacao_motor.conferir_tetos_da_meta(self._copia(mudar(10 ** 12))["meta"])
                self.assertEqual(erro.exception.code, "avaliacao_valor")


# =============================================================================
# 2. Gravação (SQLite com as migrações reais)
# =============================================================================
class _Etapa2Case(_BancoCase):
    def _resultado(self, *conteudos):
        return avaliar_como_o_worker(self.caminhos(*conteudos), hoje=HOJE)

    def _registrar(self, resultado, **campos):
        import avaliacao_cenario
        lead = avaliacao_cenario.registrar(resultado, registrado_por=self.admin_id, meses_prata=24,
                                           **({"contato": CONTATO} if "lead_id" not in campos else {}), **campos)
        self.db.session.commit()
        return lead

    def _registros(self, lead_id):
        from models import BrainstormingDadosPdv
        self.db.session.expire_all()
        return self.db.session.scalars(
            select(BrainstormingDadosPdv).where(BrainstormingDadosPdv.lead_id == lead_id)
            .order_by(BrainstormingDadosPdv.ano_exercicio, BrainstormingDadosPdv.chave_produto)).all()

    def _lead(self, lead_id):
        from models import LeadAvaliacao
        self.db.session.expire_all()
        return self.db.session.get(LeadAvaliacao, lead_id)

    def _limiares_gravados(self, lead_id):
        return json.loads(self._lead(lead_id).limiares_triagem_json)

    def _gravar_limiares(self, lead_id, texto):
        self._lead(lead_id).limiares_triagem_json = texto
        self.db.session.commit()

    def _estado(self, lead_id):
        """O que a triagem grava no lead e nas linhas: para conferir que nada foi regravado."""
        lead = self._lead(lead_id)
        linhas = sorted((str(r.id), r.bloco_triagem, r.flag_omissao_erp, r.vazamento_estimado_reais)
                        for r in self._registros(lead_id))
        return (lead.resumo_urgencia_json, lead.resumo_atencao_json, lead.resumo_eletiva_json, lead.versao_triagem,
                lead.limiares_triagem_json, lead.itens_omissao_erp, lead.atualizado_em, linhas)


class GravacaoDaEtapa2Tests(_Etapa2Case):
    def test_ida_e_volta_da_staging(self):
        import avaliacao_cenario
        self.login("admin-test@example.invalid")
        envios = {"mesmos meses de 2024 e 2025": self._resultado(*LOTE_MESMOS_MESES),
                  "cadastro de março e abril": _cadastro(self.pasta, "marco", "abril")}
        derivadas = ("bloco_triagem", "flag_venda_sem_custo", "flag_venda_abaixo_custo", "flag_omissao_erp",
                     "custo_medio_registrado", "vazamento_estimado_reais")
        do_lead = ("resumo_urgencia_json", "resumo_atencao_json", "resumo_eletiva_json", "versao_triagem",
                   "limiares_triagem_json", "itens_venda_sem_custo", "itens_venda_abaixo_custo", "itens_omissao_erp",
                   "vazamento_total_reais")
        for envio, resultado in envios.items():
            with self.subTest(envio=envio):
                identificador = self._registrar(resultado).id
                quantizadas = {(l["ano_exercicio"], l["chave_produto"]): l
                               for l in avaliacao_cenario.quantizar(resultado["linhas"])}
                relidas = {(r.ano_exercicio, r.chave_produto): avaliacao_cenario.linha_da_staging(r)
                           for r in self._registros(identificador)}
                self.assertEqual(set(relidas), set(quantizadas))
                # Cada coluna do motor volta do banco com o valor e o tipo da linha quantizada.
                for par, relida in relidas.items():
                    for coluna in avaliacao_motor.COLUNAS:
                        if coluna == "categoria":
                            continue
                        esperado = quantizadas[par][coluna]
                        self.assertEqual((type(relida[coluna]), relida[coluna]), (type(esperado), esperado),
                                         f"{par[0]} {coluna}")
                    self.assertEqual(relida["categoria_produto"], quantizadas[par]["categoria_produto"])
                    self.assertEqual((relida["lead_id"], relida["versao_regras"]),
                                     (identificador, avaliacao_motor.VERSAO_REGRAS_LEAD))

                # O diagnóstico da rota JSON, refeito da staging, tem os itens do resumo gravado em cada bloco.
                payload = self.client.get(f"{PAGINA}/{identificador}/diagnostico.json").get_json()
                self.assertEqual(payload["situacao"], "atual")
                gravado = self._lead(identificador)
                totais = {b: payload["blocos"][b]["total"] for b in triagem_cenario.BLOCOS}
                self.assertEqual(totais, {b: json.loads(getattr(gravado, f"resumo_{b}_json"))["itens"]
                                          for b in triagem_cenario.BLOCOS})
                self.assertGreater(sum(totais.values()), 0)

                def gravado_pela_triagem():
                    linhas = {r.id: tuple(getattr(r, c) for c in derivadas) for r in self._registros(identificador)}
                    lead = self._lead(identificador)
                    return linhas, tuple(getattr(lead, c) for c in do_lead)
                antes = gravado_pela_triagem()
                # A retriagem a partir da staging não muda nenhum bloco nem marca.
                avaliacao_cenario.retriar(identificador)
                self.db.session.commit()
                self.assertEqual(gravado_pela_triagem(), antes)
                # E, sem nada do que a triagem grava, ela refaz tudo igual só a partir da staging.
                self.db.session.execute(text(
                    "UPDATE brainstorming_dados_pdv SET bloco_triagem = NULL, flag_venda_sem_custo = 0, "
                    "flag_venda_abaixo_custo = 0, flag_omissao_erp = 0, custo_medio_registrado = NULL, "
                    "\"vazamento_estimado_r$\" = 0 WHERE lead_id = :id"), {"id": identificador.hex})
                self.db.session.execute(text(
                    "UPDATE lead_avaliacao SET resumo_urgencia_json = NULL, resumo_atencao_json = NULL, "
                    "resumo_eletiva_json = NULL, versao_triagem = NULL, limiares_triagem_json = NULL, "
                    "itens_venda_sem_custo = NULL, itens_venda_abaixo_custo = NULL, itens_omissao_erp = NULL, "
                    "\"vazamento_total_r$\" = NULL WHERE id = :id"), {"id": identificador.hex})
                self.db.session.commit()
                avaliacao_cenario.retriar(identificador)
                self.db.session.commit()
                self.assertEqual(gravado_pela_triagem(), antes)

    def test_cabecalho_ambiguo_nao_marca_omissao_na_gravacao_nem_na_retriagem(self):
        import avaliacao_cenario
        resultado = _cadastro(self.pasta, "cod_ncm_antes")
        # "Cód. NCM" lido como código junta os dois leites num código só: o conflito existe na linha, mas o
        # cabeçalho ambíguo do envio impede que ele vire omissão no ERP.
        self.assertEqual(resultado["meta"]["relatorios_cabecalho_ambiguo"], 1)
        self.assertEqual(sum(l["conflito_cadastral"] for l in _linhas(resultado)), 2)
        identificador = self._registrar(resultado).id

        def conferir():
            registros = self._registros(identificador)
            self.assertEqual(len(registros), 3)
            conflitos = [r for r in registros if r.conflito_cadastral]
            self.assertEqual(len(conflitos), 2)
            # Sem o cabeçalho ambíguo, a mesma linha seria omissão: a regra é o que impede a marca.
            self.assertTrue(triagem_cenario.marcas(avaliacao_cenario.linha_da_staging(conflitos[0]))["flag_omissao_erp"])
            self.assertEqual([r.flag_omissao_erp for r in registros], [False, False, False])
            self.assertEqual(self._lead(identificador).itens_omissao_erp, 0)
        conferir()
        # A retriagem refaz as marcas do zero: o que estiver gravado errado volta ao certo.
        self.db.session.execute(text("UPDATE brainstorming_dados_pdv SET flag_omissao_erp = 1"))
        self.db.session.execute(text("UPDATE lead_avaliacao SET itens_omissao_erp = 9"))
        self.db.session.commit()
        avaliacao_cenario.retriar(identificador)
        self.db.session.commit()
        conferir()

    def test_retriagem_vale_ate_o_ultimo_instante_dos_30_dias(self):
        import avaliacao_cenario
        identificador = self._registrar(self._resultado(*LOTE)).id
        envio = min(r.criado_em for r in self._registros(identificador))
        limite = envio + timedelta(days=30)
        avaliacao_cenario.retriar(identificador, agora=limite - timedelta(seconds=1))
        self.db.session.rollback()
        for agora in (limite, limite + timedelta(days=1)):
            with self.subTest(agora=agora.isoformat()):
                with self.assertRaises(avaliacao_cenario.LeadIndisponivel) as erro:
                    avaliacao_cenario.retriar(identificador, agora=agora)
                self.assertIn("30 dias", str(erro.exception))
                self.db.session.rollback()
        self.assertTrue(avaliacao_cenario.dentro_da_retencao(envio, limite - timedelta(microseconds=1)))
        self.assertFalse(avaliacao_cenario.dentro_da_retencao(envio, limite))

    def test_limiares_de_outra_versao_dao_lugar_aos_padrao_com_aviso(self):
        import avaliacao_cenario
        mensagem = avaliacao_cenario.MENSAGEM_LIMIARES_DE_OUTRA_VERSAO
        padrao = triagem_cenario.LIMIARES_PADRAO.para_dict()
        identificador = self._registrar(self._resultado(*LOTE_MESMOS_MESES)).id
        cli = self.app.test_cli_runner()

        # Retriagem: os padrão atuais (não os 0,05 e 0,40 gravados), a versão atual gravada e o aviso.
        self._gravar_limiares(identificador, _limiares_de_outra_versao())
        lead = avaliacao_cenario.retriar(identificador)
        self.db.session.commit()
        self.assertEqual(lead.aviso_limiares, mensagem)
        self.assertEqual(self._limiares_gravados(identificador), padrao)
        # Com os limiares da versão atual, nenhum aviso.
        lead = avaliacao_cenario.retriar(identificador)
        self.db.session.commit()
        self.assertIsNone(lead.aviso_limiares)

        # Reenvio sem limiares: os padrão atuais, com o mesmo aviso.
        self._gravar_limiares(identificador, _limiares_de_outra_versao())
        lead = self._registrar(self._resultado(NOV25, DEZ25), lead_id=identificador)
        self.assertEqual(lead.aviso_limiares, mensagem)
        self.assertEqual(self._limiares_gravados(identificador), padrao)

        # Comando sem --limiares: a saída traz o aviso.
        self._gravar_limiares(identificador, _limiares_de_outra_versao())
        self.db.session.remove()
        saida = cli.invoke(args=["leads", "retriar", str(identificador)])
        self.assertEqual(saida.exit_code, 0, saida.output)
        self.assertIn(f"Aviso: {mensagem}", saida.output)
        self.assertEqual(self._limiares_gravados(identificador), padrao)

        # Comando com --limiares: parte dos padrão atuais (e não dos de outra versão) e troca só o pedido.
        self._gravar_limiares(identificador, _limiares_de_outra_versao())
        self.db.session.remove()
        saida = cli.invoke(args=["leads", "retriar", str(identificador), "--limiares", '{"margem_baixa": "0.08"}'])
        self.assertEqual(saida.exit_code, 0, saida.output)
        self.assertEqual(self._limiares_gravados(identificador), {**padrao, "margem_baixa": "0.08"})

    def test_limiares_ilegiveis_recusam_a_retriagem_e_o_reenvio_usa_os_padrao(self):
        import avaliacao_cenario
        from models import BrainstormingDadosPdv
        identificador = self._registrar(self._resultado(*LOTE)).id
        truncado = '{"margem_baixa": "0.1'
        self._gravar_limiares(identificador, truncado)
        antes = self._estado(identificador)
        with self.assertRaises(avaliacao_cenario.LeadIndisponivel):
            avaliacao_cenario.retriar(identificador)
        self.db.session.rollback()
        self.assertEqual(self._estado(identificador), antes)
        lead = self._registrar(self._resultado(JAN25, FEV25), lead_id=identificador)
        self.assertIsNotNone(lead.aviso_limiares)
        self.assertIn("ilegíveis", lead.aviso_limiares)
        self.assertEqual(self._limiares_gravados(identificador), triagem_cenario.LIMIARES_PADRAO.para_dict())
        self.assertEqual(self.contar(BrainstormingDadosPdv), 3)

    def test_vazamento_total_acima_do_teto_nao_grava_nada(self):
        import avaliacao_cenario
        from models import BrainstormingDadosPdv, LeadAvaliacao
        resultado = self._resultado(*LOTE)
        posicao = avaliacao_motor.COLUNAS.index("recuperavel")
        self.assertEqual(len(resultado["linhas"]["valores"]), 6)

        def com_recuperavel(*valores):
            copia = json.loads(json.dumps(resultado))
            for linha, valor in zip(copia["linhas"]["valores"], valores):
                linha[posicao] = valor
            # Cada linha cabe na coluna (o contrato aceita): o que passa do teto é a soma do lead.
            return etl_runtime.validar_avaliacao(copia)

        def registrar(dados):
            return avaliacao_cenario.registrar(dados, contato=CONTATO, registrado_por=self.admin_id, meses_prata=24)
        # R$ 12 bilhões somados (R$ 2 bilhões por linha) e exatamente R$ 10 bilhões: NUMERIC(12,2) não guarda.
        for caso, valores in (("12 bilhões", (2e9,) * 6), ("no teto", (2e9,) * 5 + (0,))):
            with self.subTest(caso=caso):
                with self.assertRaises(ETLInputError) as erro:
                    registrar(com_recuperavel(*valores))
                self.assertEqual(erro.exception.code, "avaliacao_valor")
                self.db.session.rollback()
                self.assertEqual((self.contar(LeadAvaliacao), self.contar(BrainstormingDadosPdv)), (0, 0))
        # Um centavo abaixo do teto, grava.
        lead = registrar(com_recuperavel(2e9, 2e9, 2e9, 2e9, 1999999999.99, 0))
        self.db.session.commit()
        self.assertEqual(self._lead(lead.id).vazamento_total_reais, Decimal("9999999999.99"))

    def test_descricao_com_surrogate_direto_na_gravacao_vira_recusa(self):
        # O contrato recusaria o resultado; chamada direto (sem ele), a gravação também recusa, sem erro 500.
        import avaliacao_cenario
        from models import BrainstormingDadosPdv, LeadAvaliacao
        posicao = avaliacao_motor.COLUNAS.index("descricao_produto")

        def com_surrogate(*conteudos):
            resultado = self._resultado(*conteudos)
            resultado["linhas"]["valores"][0][posicao] = "Cafe \ud800"
            return resultado
        with self.assertRaises(ETLInputError) as erro:
            avaliacao_cenario.registrar(com_surrogate(*LOTE), contato=CONTATO, registrado_por=self.admin_id,
                                        meses_prata=24)
        self.assertEqual(erro.exception.code, "worker")
        self.db.session.rollback()
        self.assertEqual((self.contar(LeadAvaliacao), self.contar(BrainstormingDadosPdv)), (0, 0))
        # No reenvio, as linhas anteriores do lead continuam lá.
        identificador = self._registrar(self._resultado(*LOTE)).id
        antes = self._estado(identificador)
        with self.assertRaises(ETLInputError) as erro:
            avaliacao_cenario.registrar(com_surrogate(JAN25, FEV25), lead_id=identificador,
                                        registrado_por=self.admin_id, meses_prata=24)
        self.assertEqual(erro.exception.code, "worker")
        self.db.session.rollback()
        self.assertEqual(self._estado(identificador), antes)
        self.assertEqual((self.contar(BrainstormingDadosPdv), self._lead(identificador).arquivos), (6, 4))


# =============================================================================
# 3. Rotas e comandos
# =============================================================================
class RotasDaEtapa2Tests(_Etapa2Case):
    def setUp(self):
        super().setUp()
        self.worker_chamado = 0

        def worker(caminhos, limits=None):
            self.worker_chamado += 1
            return avaliar_como_o_worker(caminhos, limits, hoje=HOJE)

        self.stack.enter_context(patch("etl_runtime.run_avaliacao_cenario", side_effect=worker))
        self.login("admin-test@example.invalid")

    def _avisos(self):
        """As mensagens (categoria, texto) que a próxima página vai mostrar."""
        with self.client.session_transaction() as sessao:
            return list(sessao.get("_flashes", []))

    def _retriar_pela_tela(self, lead_id):
        detalhe = f"{PAGINA}/{lead_id}"
        return self.client.post(f"{detalhe}/retriar", data={"csrf_token": self.csrf_token(detalhe)})

    def _reenviar_pela_tela(self, lead_id, *conteudos):
        detalhe = f"{PAGINA}/{lead_id}"
        return self.client.post(f"{detalhe}/reenviar", data={
            "csrf_token": self.csrf_token(detalhe), "autorizacao": "1",
            "arquivos": [(io.BytesIO(conteudo), f"mes_{i}.csv") for i, conteudo in enumerate(conteudos, 1)]},
            content_type="multipart/form-data")

    def test_retriagem_alem_dos_30_dias_e_recusada_pela_funcao_pela_tela_e_pelo_comando(self):
        import avaliacao_cenario
        from horario import agora_utc
        from models import BrainstormingDadosPdv
        identificador = self._registrar(self._resultado(*LOTE_MESMOS_MESES)).id
        detalhe = f"{PAGINA}/{identificador}"
        botao = f'action="{detalhe}/retriar"'
        # O expurgo não roda: a recusa vale antes dele, com as linhas ainda no banco.
        with patch("blueprints.leads.expurgo_oportunista"):
            html = self.client.get(detalhe).get_data(as_text=True)
            self.assertIn(botao, html)
            self.assertIn("Refazer a triagem</button>", html)
            for registro in self.db.session.scalars(select(BrainstormingDadosPdv)):
                registro.criado_em = agora_utc() - timedelta(days=31)
            self.db.session.commit()
            # Um resumo que a retriagem trocaria, se rodasse.
            self._lead(identificador).resumo_urgencia_json = RESUMO_SENTINELA
            self.db.session.commit()
            antes = self._estado(identificador)

            with self.assertRaises(avaliacao_cenario.LeadIndisponivel) as erro:
                avaliacao_cenario.retriar(identificador)
            self.db.session.rollback()
            self.assertIn("30 dias", str(erro.exception))

            resposta = self._retriar_pela_tela(identificador)
            self.assertEqual(resposta.status_code, 303)
            self.assertIn(("warning", str(erro.exception)), self._avisos())
            self.assertIn(str(escape(str(erro.exception))), self.client.get(detalhe).get_data(as_text=True))
            self.assertEqual(self._estado(identificador), antes)

            self.db.session.remove()
            saida = self.app.test_cli_runner().invoke(args=["leads", "retriar", str(identificador)])
            self.assertNotEqual(saida.exit_code, 0)
            self.assertIn("30 dias", saida.output)
            self.assertEqual(self._estado(identificador), antes)

            html = self.client.get(detalhe).get_data(as_text=True)
        self.assertNotIn(botao, html)
        self.assertNotIn("Refazer a triagem</button>", html)
        self.assertIn("Dados de PDV indisponíveis", html)
        self.assertEqual(self.contar(BrainstormingDadosPdv), 6)

    def test_retriagem_e_reenvio_pela_tela_avisam_dos_limiares(self):
        import avaliacao_cenario
        mensagem = avaliacao_cenario.MENSAGEM_LIMIARES_DE_OUTRA_VERSAO
        identificador = self._registrar(self._resultado(*LOTE_MESMOS_MESES)).id
        detalhe = f"{PAGINA}/{identificador}"

        self._gravar_limiares(identificador, _limiares_de_outra_versao())
        self.assertEqual(self._retriar_pela_tela(identificador).status_code, 303)
        self.assertIn(("warning", mensagem), self._avisos())
        self.assertIn(str(escape(mensagem)), self.client.get(detalhe).get_data(as_text=True))
        self.assertEqual(self._limiares_gravados(identificador), triagem_cenario.LIMIARES_PADRAO.para_dict())
        # Com os limiares da versão atual, a retriagem não avisa.
        self.assertEqual(self._retriar_pela_tela(identificador).status_code, 303)
        self.assertEqual([categoria for categoria, _ in self._avisos()], ["success"])

        # Reenvio pela tela com limiares de outra versão.
        self._gravar_limiares(identificador, _limiares_de_outra_versao())
        self.assertEqual(self._reenviar_pela_tela(identificador, NOV25, DEZ25).status_code, 303)
        self.assertIn(("warning", mensagem), self._avisos())
        self.assertEqual(self._limiares_gravados(identificador), triagem_cenario.LIMIARES_PADRAO.para_dict())

        # Limiares ilegíveis: a retriagem pela tela recusa com aviso; o reenvio usa os padrão e avisa.
        self._gravar_limiares(identificador, '{"margem_baixa": "0.1')
        self.assertEqual(self._retriar_pela_tela(identificador).status_code, 303)
        self.assertEqual([(categoria, "ilegíveis" in texto) for categoria, texto in self._avisos()], [("warning", True)])
        self.client.get(detalhe)
        self.assertEqual(self._reenviar_pela_tela(identificador, NOV25, DEZ25).status_code, 303)
        self.assertEqual([(categoria, "ilegíveis" in texto) for categoria, texto in self._avisos()],
                         [("success", False), ("warning", True)])
        self.assertEqual(self._limiares_gravados(identificador), triagem_cenario.LIMIARES_PADRAO.para_dict())
        self.assertEqual(self.worker_chamado, 2)

    def test_leitura_do_diagnostico_em_json_registra_so_identificadores(self):
        identificador = self._registrar(self._resultado(*LOTE_MESMOS_MESES)).id
        # O fileConfig() do Alembic, nas migrações do setUp, desliga os loggers que já existiam.
        self.stack.enter_context(patch.object(self.app.logger, "disabled", False))
        with self.assertLogs(self.app.logger, "DEBUG") as registro:
            resposta = self.client.get(f"{PAGINA}/{identificador}/diagnostico.json")
        self.assertEqual(resposta.status_code, 200)
        mensagens = [r.getMessage() for r in registro.records]
        self.assertTrue(any(str(identificador) in mensagem for mensagem in mensagens), mensagens)
        proibidos = (*CONTATO.values(), "Leite 1L", "Cafe 500g", "Café 500g")
        for mensagem in mensagens:
            for dado in proibidos:
                self.assertNotIn(dado, mensagem)

    def test_limiares_gravados_com_nan_deixam_o_diagnostico_desatualizado_em_json_estrito(self):
        identificador = self._registrar(self._resultado(*LOTE_MESMOS_MESES)).id
        texto = json.dumps({**triagem_cenario.LIMIARES_PADRAO.para_dict(), "margem_baixa": float("nan")})
        self.assertIn("NaN", texto)
        self._gravar_limiares(identificador, texto)
        resposta = self.client.get(f"{PAGINA}/{identificador}/diagnostico.json")
        self.assertEqual(resposta.status_code, 200)
        payload = json.loads(resposta.get_data(as_text=True), parse_constant=_sem_constante)
        self.assertEqual((payload["situacao"], payload["limiares"], payload["blocos"], payload["categorias"]),
                         ("desatualizado", None, None, None))
        self.assertEqual(self.client.get(f"{PAGINA}/{identificador}").status_code, 200)

    def test_lista_sem_o_esquema_cita_a_revisao_da_etapa_2(self):
        import blueprints.leads as rotas
        with patch("blueprints.leads.esquema_disponivel", return_value=False):
            pagina = self.client.get(PAGINA)
        self.assertEqual(pagina.status_code, 200)
        html = pagina.get_data(as_text=True)
        self.assertIn(REVISAO_ETAPA_2, html)
        self.assertIn(str(escape(rotas.MENSAGEM_SEM_ESQUEMA)), html)
        self.assertNotIn("Nova avaliação", html)

    def test_resumo_e_meta_gravados_fora_do_formato_nao_derrubam_a_tela(self):
        identificador = self._registrar(self._resultado(*LOTE_MESMOS_MESES)).id
        detalhe = f"{PAGINA}/{identificador}"
        html = self.client.get(detalhe).get_data(as_text=True)
        for rotulo in ("Urgência", "Atenção", "Eletiva"):
            self.assertIn(f"{rotulo} · 1 produto(s)</h2>", html)
        gravado = self._lead(identificador)
        urgencia, atencao = json.loads(gravado.resumo_urgencia_json), json.loads(gravado.resumo_atencao_json)
        self.assertTrue(atencao["motivos"])
        gravado.resumo_urgencia_json = json.dumps({**urgencia, "itens": str(urgencia["itens"])})
        gravado.resumo_atencao_json = json.dumps({**atencao, "motivos": sorted(atencao["motivos"])})
        self.db.session.commit()
        pagina = self.client.get(detalhe)
        self.assertEqual(pagina.status_code, 200)
        html = pagina.get_data(as_text=True)
        # O resumo fora do formato vale nada; os itens do bloco continuam os refeitos da staging.
        self.assertIn('<h2 id="blocourgencia">Urgência</h2>', html)
        self.assertIn('<h2 id="blocoatencao">Atenção</h2>', html)
        self.assertIn("Eletiva · 1 produto(s)</h2>", html)
        self.assertIn("Leite 1L", html)

        # Meta com um ajuste sem venda em texto: o JSON fica "desatualizado" (só o resumo) e a tela abre.
        gravado = self._lead(identificador)
        meta = json.loads(gravado.meta_json)
        meta["ajustes_sem_venda_por_ano"]["2025"]["custo_bonificacoes"] = "abc"
        gravado.meta_json = json.dumps(meta)
        self.db.session.commit()
        resposta = self.client.get(f"{detalhe}/diagnostico.json")
        self.assertEqual(resposta.status_code, 200)
        payload = json.loads(resposta.get_data(as_text=True), parse_constant=_sem_constante)
        self.assertEqual((payload["situacao"], payload["totais"], payload["blocos"]), ("desatualizado", None, None))
        self.assertEqual(self.client.get(detalhe).status_code, 200)


# =============================================================================
# 4. Downgrade da 7c4e2a9b5d31
# =============================================================================
class DowngradeDaEtapa2Tests(_Etapa2Case):
    COLUNAS_DA_ETAPA_2 = frozenset({"itens_omissao_erp", "limiares_triagem_json"})

    def _migrar(self, comando, revisao):
        from flask_migrate import downgrade, upgrade
        funcao = {"downgrade": downgrade, "upgrade": upgrade}[comando]
        self.db.session.remove()
        # Sem o fileConfig() do Alembic, que trocaria os handlers e desligaria os loggers.
        with patch("logging.config.fileConfig"), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            funcao(directory=str(ROOT / "migrations"), revision=revisao)

    def _versao(self):
        return self.db.session.scalar(text("SELECT version_num FROM alembic_version"))

    def _colunas_do_lead(self):
        return {coluna["name"] for coluna in inspect(self.db.engine).get_columns("lead_avaliacao")}

    def test_downgrade_recusa_lead_expirado_sem_linhas_com_as_colunas_da_etapa_2(self):
        from models import BrainstormingDadosPdv, LeadAvaliacao
        limiares = json.dumps(triagem_cenario.LIMIARES_PADRAO.para_dict())
        self.db.session.add(LeadAvaliacao(**CONTATO, status="expirado", itens_omissao_erp=3,
                                          limiares_triagem_json=limiares))
        self.db.session.commit()
        self.assertEqual(self.contar(BrainstormingDadosPdv), 0)

        def gravar(itens, texto):
            self.db.session.execute(text("UPDATE lead_avaliacao SET itens_omissao_erp = :itens, "
                                         "limiares_triagem_json = :texto"), {"itens": itens, "texto": texto})
            self.db.session.commit()
        # As duas colunas e cada uma sozinha seguram o downgrade (o expurgo não as apaga).
        for itens, texto in ((3, limiares), (None, limiares), (3, None)):
            with self.subTest(itens_omissao_erp=itens, limiares_gravados=texto is not None):
                gravar(itens, texto)
                with self.assertLogs("flask_migrate", "ERROR") as registro, self.assertRaises(SystemExit):
                    self._migrar("downgrade", REVISAO_ANTERIOR)
                self.assertIn("Downgrade da Etapa 2 da Avaliação de Cenário recusado", registro.output[0])
                self.assertEqual(self._versao(), REVISAO_ETAPA_2)
                self.assertTrue(self.COLUNAS_DA_ETAPA_2 <= self._colunas_do_lead())
                self.assertEqual(tuple(self.db.session.execute(text(
                    "SELECT itens_omissao_erp, limiares_triagem_json FROM lead_avaliacao")).one()), (itens, texto))
        # Com as duas colunas anuladas, o downgrade passa e o lead fica.
        gravar(None, None)
        self._migrar("downgrade", REVISAO_ANTERIOR)
        self.assertEqual(self._versao(), REVISAO_ANTERIOR)
        self.assertFalse(self.COLUNAS_DA_ETAPA_2 & self._colunas_do_lead())
        self.assertEqual(self.db.session.execute(text("SELECT status FROM lead_avaliacao")).scalars().all(),
                         ["expirado"])


if __name__ == "__main__":
    unittest.main()
