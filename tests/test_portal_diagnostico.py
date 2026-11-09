"""Etapa 4: o diagnóstico do lead no portal do consultor.

  1. Carga das linhas sem ORM (avaliacao_cenario.linhas_do_lead): o mesmo conteúdo de linha_da_staging,
     com a assinatura que muda com qualquer valor.
  2. Cache do diagnóstico por processo (blueprints/leads._CacheDoDiagnostico): a segunda leitura não
     refaz o cálculo; reenvio, retriagem, mudança de linha, parâmetros, limite e o fim da retenção
     geram outra chave ou vencem a entrada; o TTL 0 desliga; cada aplicação tem o próprio cache.
  3. Visualizações: séries prontas para gráficos, só números e rótulos fixos (e as categorias enquanto
     as linhas existem), do cálculo atual ou do resumo gravado.
  4. Telas: o resumo executivo do caixa resgatável, a matriz de triagem, o estoque parado e excedente,
     o simulador com os parâmetros da consulta (inválidos: aviso, sem erro) e a auditoria de preço; na
     lista, a triagem, as marcas e o caixa resgatável de cada lead.

Dados fictícios.
"""

from datetime import date, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
import uuid

from sqlalchemy import select, text

import avaliacao_cenario
import diagnostico_cenario
import triagem_cenario
from horario import agora_utc
from tests.deploy_support import DeployTestCase
from tests.raio_x_support import avaliar_como_o_worker
from tests.test_avaliacao_cenario import CONTATO, _relatorio

PAGINA = "/admin/leads"
# Novembro e dezembro de 2024 e de 2025, com estoque: o Leite cai 75% nos mesmos meses (Urgência).
NOV24 = _relatorio(2024, 11, ["1;Cafe 500g;10;20,00;200,00;150,00;30", "2;Acucar 1kg;5;6,00;30,00;;12",
                              "3;Leite 1L;20;5,00;100,00;110,00;80"])
DEZ24 = _relatorio(2024, 12, ["1;Cafe 500g;10;20,00;200,00;150,00;28", "2;Acucar 1kg;5;6,00;30,00;;10",
                              "3;Leite 1L;20;5,00;100,00;90,00;75"])
NOV25 = _relatorio(2025, 11, ["1;Café 500g;8;20,00;150,00;120,00;26", "3;Leite 1L;5;5,00;25,00;20,00;70"])
DEZ25 = _relatorio(2025, 12, ["1;Café 500g;4;20,00;80,00;60,00;24", "3;Leite 1L;5;5,00;25,00;20,00;68",
                              "4;Arroz 5kg;2;27,90;55,80;44,00;9"])
LOTE = (NOV24, DEZ24, NOV25, DEZ25)


def _sem_saldo(ano, mes, linhas):
    """O relatório com a coluna de estoque e todas as células dela vazias."""
    return _relatorio(ano, mes, [";".join(l.split(";")[:-1] + [""]) for l in linhas])


# Os meses do LOTE com a coluna de estoque vazia em todas as linhas.
VAZIO = (_sem_saldo(2024, 11, ["1;Cafe 500g;10;20,00;200,00;150,00;0", "3;Leite 1L;20;5,00;100,00;110,00;0"]),
         _sem_saldo(2024, 12, ["1;Cafe 500g;10;20,00;200,00;150,00;0", "3;Leite 1L;20;5,00;100,00;90,00;0"]),
         _sem_saldo(2025, 11, ["1;Café 500g;8;20,00;150,00;120,00;0", "3;Leite 1L;5;5,00;25,00;20,00;0"]),
         _sem_saldo(2025, 12, ["1;Café 500g;4;20,00;80,00;60,00;0", "3;Leite 1L;5;5,00;25,00;20,00;0"]))


class _PortalCase(DeployTestCase):
    def setUp(self):
        super().setUp()
        self.fixtures()
        pasta = tempfile.TemporaryDirectory(prefix="nexo-portal-")
        self.addCleanup(pasta.cleanup)
        self.pasta = Path(pasta.name)

    def caminhos(self, *conteudos):
        saida = []
        for conteudo in conteudos:
            caminho = self.pasta / f"arquivo_{uuid.uuid4().hex[:8]}.csv"
            caminho.write_bytes(conteudo)
            saida.append(str(caminho))
        return saida

    def registrar(self, *conteudos, contato=CONTATO):
        resultado = avaliar_como_o_worker(self.caminhos(*(conteudos or LOTE)))
        lead = avaliacao_cenario.registrar(resultado, contato=dict(contato), registrado_por=self.admin_id,
                                           meses_prata=24)
        self.db.session.commit()
        return lead

    def diagnostico(self, lead, limite=30, parametros=None):
        from blueprints import leads as rotas
        from models import LeadAvaliacao
        with self.app.test_request_context():
            return rotas._diagnostico(self.db.session.get(LeadAvaliacao, lead.id), limite, parametros)


# =============================================================================
# 1. Carga das linhas sem ORM
# =============================================================================
class CargaTests(_PortalCase):
    def test_mesmo_conteudo_da_linha_da_staging(self):
        from models import BrainstormingDadosPdv
        lead = self.registrar()
        linhas, assinatura = avaliacao_cenario.linhas_do_lead(lead.id)
        registros = self.db.session.scalars(select(BrainstormingDadosPdv).where(BrainstormingDadosPdv.lead_id == lead.id)
                                            .order_by(BrainstormingDadosPdv.ano_exercicio,
                                                      BrainstormingDadosPdv.chave_produto)).all()
        self.assertEqual(len(linhas), len(registros))
        for linha, registro in zip(linhas, registros):
            esperado = avaliacao_cenario.linha_da_staging(registro)
            esperado.update(bloco_triagem=registro.bloco_triagem, vazamento_estimado_reais=registro.vazamento_estimado_reais)
            self.assertEqual(linha, esperado)
        self.assertIsInstance(linhas[0]["quantidade"], Decimal)
        self.assertIsInstance(linhas[0]["fim_ultimo_relatorio_com_venda"], date)
        # A assinatura muda com qualquer valor de qualquer linha, e só com isso.
        self.assertEqual(avaliacao_cenario.linhas_do_lead(lead.id)[1], assinatura)
        self.db.session.execute(text("UPDATE brainstorming_dados_pdv SET estoque_final = estoque_final + 1 "
                                     "WHERE descricao_produto = 'Arroz 5kg'"))
        self.db.session.commit()
        self.assertNotEqual(avaliacao_cenario.linhas_do_lead(lead.id)[1], assinatura)

    def test_so_as_linhas_do_lead(self):
        primeiro, segundo = self.registrar(), self.registrar(NOV25, DEZ25, contato={**CONTATO, "email": "b@example.invalid"})
        linhas, _ = avaliacao_cenario.linhas_do_lead(primeiro.id)
        self.assertEqual({l["lead_id"] for l in linhas}, {primeiro.id})
        self.assertEqual(avaliacao_cenario.linhas_do_lead(uuid.uuid4()), ([], hashlib.sha256(b"[]").hexdigest()))
        self.assertNotEqual(avaliacao_cenario.linhas_do_lead(segundo.id)[1], avaliacao_cenario.linhas_do_lead(primeiro.id)[1])

    def test_assinatura_distingue_valores_que_o_hash_do_python_iguala(self):
        lead = self.registrar()
        # hash(-1) == hash(-2) no CPython (e nos Decimal): a assinatura não pode depender disso.
        self.db.session.execute(text("UPDATE brainstorming_dados_pdv SET estoque_final = -1 WHERE descricao_produto = 'Arroz 5kg'"))
        self.db.session.commit()
        antes = avaliacao_cenario.linhas_do_lead(lead.id)[1]
        primeiro = self.diagnostico(lead)
        self.db.session.execute(text("UPDATE brainstorming_dados_pdv SET estoque_final = -2 WHERE descricao_produto = 'Arroz 5kg'"))
        self.db.session.commit()
        self.assertNotEqual(avaliacao_cenario.linhas_do_lead(lead.id)[1], antes)
        self.assertIsNot(self.diagnostico(lead), primeiro)


# =============================================================================
# 2. Cache do diagnóstico
# =============================================================================
class CacheTests(_PortalCase):
    def fim_da_retencao(self, lead):
        """O fim da retenção calculado à parte do código do cache: o menor criado_em das linhas, lido por
        SQL, mais os 30 dias (leads_avaliacao.RETENCAO_DIAS)."""
        from datetime import datetime, timezone
        from leads_avaliacao import RETENCAO_DIAS
        bruto = self.db.session.scalar(text("SELECT min(criado_em) FROM brainstorming_dados_pdv WHERE lead_id = :i"),
                                       {"i": lead.id.hex})
        instante = bruto if isinstance(bruto, datetime) else datetime.fromisoformat(str(bruto))
        instante = instante if instante.tzinfo else instante.replace(tzinfo=timezone.utc)
        return instante + timedelta(days=RETENCAO_DIAS)

    def contar(self):
        """Patch de montar_payload que conta as chamadas (o cálculo de verdade continua)."""
        return patch.object(diagnostico_cenario, "montar_payload", wraps=diagnostico_cenario.montar_payload)

    def test_segunda_leitura_vem_do_cache_e_mudancas_geram_outra_chave(self):
        import inteligencia_estoque
        lead = self.registrar()
        with self.contar() as montar:
            primeiro = self.diagnostico(lead)
            self.assertIs(self.diagnostico(lead), primeiro)
            self.assertEqual(montar.call_count, 1)
            # Outro limite e outros parâmetros: outra chave.
            self.diagnostico(lead, limite=5)
            self.diagnostico(lead, parametros=inteligencia_estoque.PARAMETROS_PADRAO.com_ajustes({"descontos": "0.5"}))
            self.assertEqual(montar.call_count, 3)
            # Retriagem com os mesmos limiares não muda nada que o payload use: vale a mesma entrada.
            avaliacao_cenario.retriar(lead.id)
            self.db.session.commit()
            self.diagnostico(lead)
            self.assertEqual(montar.call_count, 3)
            # Com outros limiares, o lead muda, e a leitura seguinte refaz.
            avaliacao_cenario.retriar(lead.id, triagem_cenario.LIMIARES_PADRAO.com_ajustes({"margem_baixa": "0.2"}))
            self.db.session.commit()
            self.diagnostico(lead)
            self.assertEqual(montar.call_count, 4)
            # Mudança direta numa linha (sem tocar no lead): a assinatura muda.
            self.db.session.execute(text("UPDATE brainstorming_dados_pdv SET estoque_final = 1"))
            self.db.session.commit()
            atualizado = self.diagnostico(lead)
            self.assertEqual(montar.call_count, 5)
            self.assertNotEqual(atualizado.payload["inteligencia"], primeiro.payload["inteligencia"])

    def test_fim_da_retencao_vence_a_entrada(self):
        # TTL de anos: só o limite da retenção pode vencer a entrada (o TTL sozinho não venceria).
        self.app.config["LEADS_DIAGNOSTICO_CACHE_SEGUNDOS"] = 10 ** 8
        lead = self.registrar()
        primeiro = self.diagnostico(lead)
        self.assertEqual(primeiro.payload["situacao"], "atual")
        fim = self.fim_da_retencao(lead)
        self.assertEqual(primeiro.expira_em, fim)
        self.assertEqual(primeiro.payload["dados_pdv_ate"], fim.isoformat())
        # Até o último instante da retenção, a mesma entrada; no instante em que ela acaba, outra leitura.
        with patch("blueprints.leads.agora_utc", return_value=fim - timedelta(microseconds=1)):
            self.assertIs(self.diagnostico(lead), primeiro)
        with patch("blueprints.leads.agora_utc", return_value=fim):
            vencido = self.diagnostico(lead)
        self.assertEqual(vencido.payload["situacao"], "expirado")
        self.assertNotIn("Leite", json.dumps(vencido.payload, ensure_ascii=False, default=str))

    def test_ttl_zero_desliga_e_cada_aplicacao_tem_o_proprio_cache(self):
        lead = self.registrar()
        self.app.config["LEADS_DIAGNOSTICO_CACHE_SEGUNDOS"] = 0
        with patch.object(diagnostico_cenario, "montar_payload", wraps=diagnostico_cenario.montar_payload) as montar:
            self.diagnostico(lead)
            self.diagnostico(lead)
        self.assertEqual(montar.call_count, 2)
        self.assertNotIn("nexo_diagnostico_leads", self.app.extensions)
        self.app.config["LEADS_DIAGNOSTICO_CACHE_SEGUNDOS"] = 600
        self.diagnostico(lead)
        self.assertEqual(len(self.app.extensions["nexo_diagnostico_leads"]), 1)

    def test_desatualizado_no_cache_tambem_vence_na_retencao(self):
        # O lead de outra versão das regras guarda nomes de produto (itens no bloco gravado): o mesmo teto.
        self.app.config["LEADS_DIAGNOSTICO_CACHE_SEGUNDOS"] = 10 ** 8
        lead = self.registrar()
        self.db.session.execute(text("UPDATE brainstorming_dados_pdv SET versao_regras = 'lead-antiga'"))
        self.db.session.commit()
        primeiro = self.diagnostico(lead)
        self.assertEqual(primeiro.payload["situacao"], "desatualizado")
        self.assertIn("Leite 1L", [i["produto"] for itens in primeiro.itens_gravados.values() for i in itens])
        fim = self.fim_da_retencao(lead)
        self.assertEqual(primeiro.expira_em, fim)
        with patch("blueprints.leads.agora_utc", return_value=fim - timedelta(microseconds=1)):
            self.assertIs(self.diagnostico(lead), primeiro)
        with patch("blueprints.leads.agora_utc", return_value=fim):
            vencido = self.diagnostico(lead)
        self.assertEqual((vencido.payload["situacao"], vencido.itens_gravados), ("expirado", None))

    def test_limite_de_entradas_varredura_e_descarte(self):
        from blueprints.leads import CACHE_MAXIMO, _CacheDoDiagnostico
        cache = _CacheDoDiagnostico()
        agora = agora_utc()
        for i in range(CACHE_MAXIMO + 3):
            cache.guardar((f"lead-{i}", i), f"diagnostico {i}", agora + timedelta(minutes=5), agora)
        self.assertEqual(len(cache), CACHE_MAXIMO)
        self.assertIsNone(cache.obter(("lead-0", 0), agora))
        ultima = (f"lead-{CACHE_MAXIMO + 2}", CACHE_MAXIMO + 2)
        self.assertEqual(cache.obter(ultima, agora), f"diagnostico {CACHE_MAXIMO + 2}")
        # Toda entrada vencida sai da memória no primeiro uso do cache, não só a que foi pedida.
        self.assertIsNone(cache.obter(("outra",), agora + timedelta(minutes=6)))
        self.assertEqual(len(cache), 0)
        cache.guardar(("vencida",), "x", agora + timedelta(minutes=1), agora)
        cache.guardar(("nova",), "y", agora + timedelta(minutes=9), agora + timedelta(minutes=2))
        self.assertEqual(len(cache), 1)
        # Descarte por lead e limpeza.
        cache.guardar(("a", 1), "a1", agora + timedelta(minutes=5), agora)
        cache.guardar(("a", 2), "a2", agora + timedelta(minutes=5), agora)
        cache.guardar(("b", 1), "b1", agora + timedelta(minutes=5), agora)
        self.assertEqual(cache.descartar_lead("a"), 2)
        self.assertEqual((cache.obter(("a", 1), agora), cache.obter(("b", 1), agora)), (None, "b1"))
        cache.limpar()
        self.assertEqual(len(cache), 0)

    def test_leitura_concorrente_nao_guarda_o_dado_de_antes_do_descarte(self):
        from blueprints import leads as rotas
        lead = self.registrar()
        original = diagnostico_cenario.montar_payload

        def montar_com_reenvio_no_meio(*args, **kwargs):
            # O reenvio (ou a retriagem) termina enquanto esta leitura calcula com as linhas de antes.
            rotas._descartar_do_cache(lead.id)
            return original(*args, **kwargs)

        with patch.object(diagnostico_cenario, "montar_payload", side_effect=montar_com_reenvio_no_meio):
            self.diagnostico(lead)
        cache = self.app.extensions["nexo_diagnostico_leads"]
        self.assertEqual(len(cache), 0)
        # Sem descarte no meio, guarda normalmente; a limpeza do expurgo também barra a leitura em curso.
        self.diagnostico(lead)
        self.assertEqual(len(cache), 1)
        cache.limpar()

        def montar_com_expurgo_no_meio(*args, **kwargs):
            rotas._descartar_do_cache()
            return original(*args, **kwargs)

        with patch.object(diagnostico_cenario, "montar_payload", side_effect=montar_com_expurgo_no_meio):
            self.diagnostico(lead)
        self.assertEqual(len(cache), 0)
        # O descarte durante a CARGA das linhas (o reenvio faz commit e descarta enquanto a leitura ainda
        # calcula a assinatura): a geração lida antes da carga barra a gravação.
        carga_original = avaliacao_cenario.linhas_do_lead

        def carga_com_reenvio_no_meio(lead_id):
            resultado = carga_original(lead_id)
            rotas._descartar_do_cache(lead_id)
            return resultado

        with patch.object(avaliacao_cenario, "linhas_do_lead", side_effect=carga_com_reenvio_no_meio):
            self.diagnostico(lead)
        self.assertEqual(len(cache), 0)

    def test_qualquer_pedido_varre_as_entradas_vencidas(self):
        lead = self.registrar()
        self.diagnostico(lead)
        cache = self.app.extensions["nexo_diagnostico_leads"]
        agora = agora_utc()
        # Uma entrada já vencida (como a de um lead que o pg_cron expurgou): sai no próximo pedido de qualquer
        # tela, mesmo sem leitura de diagnóstico.
        cache.guardar(("lead-expurgado-pelo-pg-cron", 1), "dado vencido", agora - timedelta(seconds=1),
                      agora - timedelta(seconds=2))
        self.assertEqual(len(cache), 2)
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        self.assertEqual(len(cache), 1)

    def test_reenvio_retriagem_e_expurgo_tiram_o_dado_antigo_do_cache(self):
        import io
        from models import BrainstormingDadosPdv
        import blueprints.leads as rotas
        lead = self.registrar()
        self.login("admin-test@example.invalid")
        detalhe = f"{PAGINA}/{lead.id}"
        with patch("blueprints.leads.expurgo_oportunista"):
            self.assertIn("Leite 1L", self.client.get(detalhe).get_data(as_text=True))
        cache = self.app.extensions["nexo_diagnostico_leads"]
        self.assertEqual(len(cache), 1)
        # Retriagem: o lead sai do cache na hora.
        self.assertEqual(self.client.post(f"{detalhe}/retriar", data={"csrf_token": self.csrf_token(detalhe)}).status_code, 303)
        self.assertEqual(len(cache), 0)
        # Reenvio sem o Leite: o diagnóstico com o Leite não fica guardado.
        with patch("blueprints.leads.expurgo_oportunista"):
            self.client.get(detalhe)
        self.assertEqual(len(cache), 1)
        sem_leite = (_relatorio(2025, 11, ["1;Café 500g;8;20,00;150,00;120,00;26"]),
                     _relatorio(2025, 12, ["1;Café 500g;4;20,00;80,00;60,00;24"]))
        dados = {"csrf_token": self.csrf_token(detalhe), "autorizacao": "1",
                 "arquivos": [(io.BytesIO(c), f"mes_{i}.csv") for i, c in enumerate(sem_leite, 1)]}
        with patch("etl_runtime.run_avaliacao_cenario", side_effect=lambda caminhos, limits=None: avaliar_como_o_worker(caminhos, limits)):
            resposta = self.client.post(f"{detalhe}/reenviar", data=dados, content_type="multipart/form-data")
        self.assertEqual(resposta.status_code, 303, resposta.get_data(as_text=True)[:300])
        self.assertEqual(len(cache), 0)
        # Expurgo pela tela: as linhas passam dos 30 dias, e o cache inteiro sai.
        with patch("blueprints.leads.expurgo_oportunista"):
            self.client.get(detalhe)
        self.assertEqual(len(cache), 1)
        for registro in self.db.session.scalars(select(BrainstormingDadosPdv)):
            registro.criado_em = agora_utc() - timedelta(days=31)
        self.db.session.commit()
        with patch.dict(rotas._expurgo_ultimo, {"instante": None}):
            self.client.get(PAGINA)
        self.assertEqual(len(cache), 0)

    def test_rota_le_do_cache_e_registra_cada_leitura(self):
        lead = self.registrar()
        self.login("admin-test@example.invalid")
        caminho = f"{PAGINA}/{lead.id}/diagnostico.json"
        # No logger da aplicação, como os demais testes de log das rotas: o fileConfig() do Alembic, nas
        # migrações do setUp, desliga os loggers que já existiam (na suíte inteira, o da aplicação).
        self.stack.enter_context(patch.object(self.app.logger, "disabled", False))
        with self.contar() as montar, self.assertLogs(self.app.logger, "INFO") as registro:
            primeira = self.client.get(caminho)
            segunda = self.client.get(caminho)
        self.assertEqual((primeira.status_code, segunda.status_code), (200, 200))
        self.assertEqual(primeira.get_json(), segunda.get_json())
        self.assertEqual(montar.call_count, 1)
        # Cada leitura fica no log, mesmo vinda do cache (só identificadores).
        self.assertEqual(sum("lido pelo consultor" in linha for linha in registro.output), 2)


# =============================================================================
# 3. Visualizações
# =============================================================================
class VisualizacoesTests(_PortalCase):
    def test_series_do_payload_atual(self):
        lead = self.registrar()
        payload = self.diagnostico(lead).payload
        self.assertEqual(payload["versao"], "diagnostico-lead-2026-11.3")
        visualizacoes = payload["visualizacoes"]
        self.assertTrue({"triagem", "marcas", "caixa_resgatavel", "estoque_por_idade", "simulador"} <= set(visualizacoes))
        for nome, grafico in visualizacoes.items():
            with self.subTest(grafico=nome):
                self.assertIn(grafico["tipo"], ("barras", "linhas"))
                self.assertTrue(grafico["titulo"])
                for serie in grafico["series"]:
                    self.assertEqual(len(serie["valores"]), len(grafico["rotulos"]))
                    self.assertIn(serie["unidade"], ("BRL", "itens", "fracao"))
                    self.assertTrue(all(v is None or (type(v) in (int, float)) for v in serie["valores"]))
        self.assertEqual(visualizacoes["triagem"]["rotulos"], ["Urgência", "Atenção", "Eletiva"])
        caixa = visualizacoes["caixa_resgatavel"]
        self.assertEqual(caixa["naturezas"], ["retroativo", "capital_parado", "capital_liberavel", "projecao"])
        self.assertEqual(caixa["series"][0]["valores"][0], payload["lead"]["potencial_recuperavel_total"])
        self.assertEqual(visualizacoes["simulador"]["rotulos"], ["0%", "10%", "20%", "30%"])
        texto = json.dumps(visualizacoes, ensure_ascii=False)
        for proibido in ("lead@example.invalid", "Contato fictício", "Mercado fictício"):
            self.assertNotIn(proibido, texto)

    def test_sem_as_linhas_usa_o_resumo_gravado(self):
        lead = self.registrar()
        gravado = json.loads(lead.resumo_inteligencia_json)
        self.db.session.execute(text("DELETE FROM brainstorming_dados_pdv"))
        self.db.session.commit()
        payload = self.diagnostico(lead).payload
        self.assertEqual(payload["situacao"], "expirado")
        self.assertEqual(payload["resumo_inteligencia"], gravado)
        caixa = payload["visualizacoes"]["caixa_resgatavel"]
        primeira = min(gravado["capital_parado"], key=int)
        self.assertEqual(caixa["series"][0]["valores"][1], gravado["capital_parado"][primeira])
        self.assertEqual(caixa["series"][0]["valores"][2], gravado["capital_excedente_liberavel"])
        self.assertNotIn("estoque_por_idade", payload["visualizacoes"])
        # Expirado: nenhum texto do PDV.
        self.assertNotIn("Leite", json.dumps(payload, ensure_ascii=False, default=str))

    def test_resumo_gravado_fora_do_formato_vale_nada(self):
        import inteligencia_estoque
        from blueprints.leads import _resumo_inteligencia
        lead = self.registrar()
        versao = inteligencia_estoque.VERSAO_INTELIGENCIA
        # Outra versão, mesmo no formato certo, também vale nada (o sentido de um campo pode ter mudado). Os
        # casos de formato usam a versão do módulo: valem ainda depois da próxima troca de versão.
        for gravado in ('{"versao": "outra"}', '{"versao": "inteligencia-lead-2026-10.9", "com_estoque": false}',
                        json.dumps({"versao": versao, "com_estoque": "sim"}), "[1, 2]", "nao e json",
                        json.dumps({"versao": versao, "com_estoque": True, "capital_parado": {"sessenta": 1}}),
                        json.dumps({"versao": versao, "com_estoque": True, "capital_parado": {"abc": 1}}),
                        json.dumps({"versao": versao, "com_estoque": True, "capital_parado": {"6a": 1}}),
                        json.dumps({"versao": versao, "com_estoque": True, "capital_parado": {"\u00b2": 1}}),
                        json.dumps({"versao": versao, "com_estoque": True, "itens_parados": {"12345": 1}}),
                        json.dumps({"versao": versao, "com_estoque": True, "capital_em_estoque": "10"}),
                        json.dumps({"versao": versao, "com_estoque": True, "capital_em_estoque": -1}),
                        # O formato da 11.1 (antes da contagem dos produtos sem saldo já parados): vale nada.
                        '{"versao": "inteligencia-lead-2026-11.1", "com_estoque": true, "capital_parado": {"60": 0.0}}',
                        json.dumps({"versao": versao, "com_estoque": True, "itens_parados_sem_estoque_informado": 1.5}),
                        json.dumps({"versao": versao, "com_estoque": True, "itens_parados_sem_estoque_informado": "1"}),
                        json.dumps({"versao": versao, "com_estoque": True, "itens_sem_estoque_com_custo": -1}),
                        # Com estoque informado, sem uma das contagens (o resumo diria "soma completa"): vale nada.
                        json.dumps({"versao": versao, "com_estoque": True, "capital_parado": {"60": 0.0},
                                    "itens_sem_estoque_informado": 1, "itens_parados_sem_estoque_informado": 1}),
                        json.dumps({"versao": versao, "com_estoque": True, "itens_sem_estoque_informado": 1,
                                    "itens_sem_estoque_com_custo": None, "itens_parados_sem_estoque_informado": 0}),
                        # Sem estoque informado, contagem que não é nula também.
                        json.dumps({"versao": versao, "com_estoque": False, "itens_sem_estoque_com_custo": 2}),
                        '{"versao": "%s", "com_estoque": true, "capital_em_estoque": 1%s}' % (versao, "0" * 400)):
            with self.subTest(gravado=gravado):
                lead.resumo_inteligencia_json = gravado
                self.assertIsNone(_resumo_inteligencia(lead))
        # O formato que o código grava vale: com estoque, as três contagens; sem estoque, nulas ou ausentes.
        contagens = {"itens_sem_estoque_informado": 1, "itens_sem_estoque_com_custo": 0,
                     "itens_parados_sem_estoque_informado": 0}
        for gravado in (json.dumps({"versao": versao, "com_estoque": True, **contagens}),
                        json.dumps({"versao": versao, "com_estoque": False}),
                        json.dumps({"versao": versao, "com_estoque": False, **dict.fromkeys(contagens)})):
            with self.subTest(valido=gravado):
                lead.resumo_inteligencia_json = gravado
                self.assertIsNotNone(_resumo_inteligencia(lead))
        self.db.session.rollback()

    def test_faixa_fora_do_formato_no_resumo_gravado_nao_derruba_o_detalhe(self):
        import inteligencia_estoque
        lead = self.registrar()
        lead.resumo_inteligencia_json = json.dumps({"versao": inteligencia_estoque.VERSAO_INTELIGENCIA, "com_estoque": True,
                                                    "capital_parado": {"abc": 1.0}})
        self.db.session.execute(text("DELETE FROM brainstorming_dados_pdv"))
        self.db.session.commit()
        self.login("admin-test@example.invalid")
        with patch("blueprints.leads.expurgo_oportunista"):
            for caminho in (PAGINA, f"{PAGINA}/{lead.id}", f"{PAGINA}/{lead.id}/diagnostico.json"):
                with self.subTest(caminho=caminho):
                    self.assertEqual(self.client.get(caminho).status_code, 200)

    def test_resumo_de_outra_versao_nao_aparece_no_detalhe_nem_no_json(self):
        lead = self.registrar()
        gravado = json.loads(lead.resumo_inteligencia_json)
        lead.resumo_inteligencia_json = json.dumps({**gravado, "versao": "inteligencia-lead-2026-10.9",
                                                    "capital_parado": {"60": 98765.43},
                                                    "capital_excedente_liberavel": 13579.24})
        # O resumo de bloco com "itens" em texto também é recusado nas séries.
        lead.resumo_atencao_json = json.dumps({**json.loads(lead.resumo_atencao_json), "itens": "1"})
        self.db.session.execute(text("DELETE FROM brainstorming_dados_pdv"))
        self.db.session.commit()
        self.login("admin-test@example.invalid")
        with patch("blueprints.leads.expurgo_oportunista"):
            html = self.client.get(f"{PAGINA}/{lead.id}").get_data(as_text=True)
            payload = self.client.get(f"{PAGINA}/{lead.id}/diagnostico.json").get_json()
        self.assertNotIn("98.765,43", html)
        self.assertNotIn("13.579,24", html)
        self.assertIsNone(payload["resumo_inteligencia"])
        self.assertEqual(payload["visualizacoes"]["caixa_resgatavel"]["series"][0]["valores"][1:3], [None, None])
        self.assertIsNone(payload["visualizacoes"]["triagem"]["series"][0]["valores"][1])


# =============================================================================
# 4. Telas
# =============================================================================
class TelasTests(_PortalCase):
    def test_detalhe_mostra_o_resumo_executivo_a_triagem_e_a_inteligencia(self):
        lead = self.registrar()
        self.login("admin-test@example.invalid")
        html = self.client.get(f"{PAGINA}/{lead.id}").get_data(as_text=True)
        for texto in ("Resumo executivo: caixa resgatável", "não se somam", "Matriz de triagem", "Motivos mais frequentes",
                      "Itens sem custo em nenhuma venda", "Itens com alguma venda abaixo do custo",
                      "Itens com omissão no ERP", "Estoque parado e excedente", "Estoque excedente",
                      "Simulador de liberação de caixa (Urgência)", "Auditoria de preço", 'name="descontos"',
                      'value="0,0.10,0.20,0.30"'):
            self.assertIn(texto, html)
        self.assertIn("Leite 1L", html)

    def test_simulador_pela_consulta_e_parametro_invalido(self):
        lead = self.registrar()
        self.login("admin-test@example.invalid")
        html = self.client.get(f"{PAGINA}/{lead.id}?descontos=0.5&fracao_liquidada=0.25").get_data(as_text=True)
        self.assertIn("50,0%", html)
        self.assertIn('value="0.5"', html)
        self.assertIn('value="0.25"', html)
        resposta = self.client.get(f"{PAGINA}/{lead.id}?descontos=abc")
        self.assertEqual(resposta.status_code, 200)
        invalida = resposta.get_data(as_text=True)
        self.assertIn("Parâmetros não aplicados.", invalida)
        self.assertIn('value="0,0.10,0.20,0.30"', invalida)

    def test_lead_expirado_mostra_o_resumo_gravado(self):
        lead = self.registrar()
        self.db.session.execute(text("DELETE FROM brainstorming_dados_pdv"))
        self.db.session.commit()
        self.login("admin-test@example.invalid")
        with patch("blueprints.leads.expurgo_oportunista"):
            html = self.client.get(f"{PAGINA}/{lead.id}").get_data(as_text=True)
        self.assertIn("Resumo executivo: caixa resgatável", html)
        self.assertIn("Valores do resumo gravado no envio de", html)
        self.assertIn("Matriz de triagem", html)
        for ausente in ("Estoque parado e excedente", "Simulador de liberação de caixa", "Leite 1L"):
            self.assertNotIn(ausente, html)

    def test_equilibrio_negativo_e_estoque_fracionario(self):
        # Na Urgência, o Leite passa a vender abaixo do custo (custo 30 para venda 25). O Arroz, no excedente
        # (2 vendidos em 61 dias), tem estoque fracionário de 9,5.
        nov25 = _relatorio(2025, 11, ["1;Café 500g;8;20,00;150,00;120,00;26", "3;Leite 1L;5;5,00;25,00;30,00;70"])
        dez25 = _relatorio(2025, 12, ["1;Café 500g;4;20,00;80,00;60,00;24", "3;Leite 1L;5;5,00;25,00;30,00;68",
                                      "4;Arroz 5kg;2;27,90;55,80;44,00;9,5"])
        lead = self.registrar(NOV24, DEZ24, nov25, dez25)
        self.login("admin-test@example.invalid")
        payload = self.client.get(f"{PAGINA}/{lead.id}/diagnostico.json").get_json()
        simulador = payload["inteligencia"]["simulador"]
        self.assertLess(simulador["desconto_de_equilibrio"], 0)
        html = self.client.get(f"{PAGINA}/{lead.id}").get_data(as_text=True)
        self.assertIn("mesmo sem desconto o caixa fica abaixo do custo", html)
        self.assertNotIn("empata com o custo com -", html)
        # Estoque fracionário com as casas (9,50), não arredondado para 10.
        self.assertIn('<td class="text-end fw-num">9,50</td>', html)

    def test_equilibrio_negativo_perto_de_zero(self):
        # Custo de 25,01 para venda de 25,00: o equilíbrio arredondado é quase zero, mas negativo.
        nov25 = _relatorio(2025, 11, ["1;Café 500g;8;20,00;150,00;120,00;26", "3;Leite 1L;5;5,00;25,00;25,01;70"])
        dez25 = _relatorio(2025, 12, ["1;Café 500g;4;20,00;80,00;60,00;24", "3;Leite 1L;5;5,00;25,00;25,01;68"])
        lead = self.registrar(NOV24, DEZ24, nov25, dez25)
        self.login("admin-test@example.invalid")
        simulador = self.client.get(f"{PAGINA}/{lead.id}/diagnostico.json").get_json()["inteligencia"]["simulador"]
        self.assertTrue(simulador["abaixo_do_custo_sem_desconto"])
        html = self.client.get(f"{PAGINA}/{lead.id}").get_data(as_text=True)
        self.assertIn("mesmo sem desconto o caixa fica abaixo do custo (só empataria vendendo menos de 0,1% acima", html)
        self.assertNotIn("-0,0%", html)

    def test_equilibrio_arredondado_a_zero_na_tela(self):
        # Escala de milhares: custo 25.001,00 para venda 25.000,00 (equilíbrio -0,00004, arredondado a 0,0).
        relatorios = (
            _relatorio(2024, 11, ["1;Cafe 500g;10;20,00;200,00;150,00;30", "3;Leite 1L;20;5000,00;100000,00;110000,00;80"]),
            _relatorio(2024, 12, ["1;Cafe 500g;10;20,00;200,00;150,00;28", "3;Leite 1L;20;5000,00;100000,00;90000,00;75"]),
            _relatorio(2025, 11, ["1;Café 500g;8;20,00;150,00;120,00;26", "3;Leite 1L;5;5000,00;25000,00;25001,00;70"]),
            _relatorio(2025, 12, ["1;Café 500g;4;20,00;80,00;60,00;24", "3;Leite 1L;5;5000,00;25000,00;25001,00;68"]))
        lead = self.registrar(*relatorios)
        self.login("admin-test@example.invalid")
        simulador = self.client.get(f"{PAGINA}/{lead.id}/diagnostico.json").get_json()["inteligencia"]["simulador"]
        self.assertEqual((simulador["desconto_de_equilibrio"], simulador["abaixo_do_custo_sem_desconto"]), (0.0, True))
        html = self.client.get(f"{PAGINA}/{lead.id}").get_data(as_text=True)
        self.assertIn("mesmo sem desconto o caixa fica abaixo do custo", html)
        self.assertNotIn("empata com o custo com 0,0%", html)

    def test_coluna_de_estoque_vazia_e_estoque_parcial(self):
        lead = self.registrar(*VAZIO)
        self.login("admin-test@example.invalid")
        payload = self.client.get(f"{PAGINA}/{lead.id}/diagnostico.json").get_json()
        self.assertIsNone(payload["inteligencia"]["estoque"])
        self.assertTrue(any("não trazem estoque informado" in a for a in payload["avisos"]))
        self.assertEqual(payload["visualizacoes"]["caixa_resgatavel"]["series"][0]["valores"][1:], [None, None, None])
        self.assertNotIn("parado há", self.client.get(PAGINA).get_data(as_text=True))
        # A matriz diz o mesmo que o aviso: estoque parado não informado (—), nunca R$ 0,00.
        self.assertIsNone(payload["totais"]["estoque_parado"])
        detalhe = self.client.get(f"{PAGINA}/{lead.id}").get_data(as_text=True)
        self.assertIn("estoque parado: —", detalhe)
        self.assertNotIn("estoque parado: R$ 0,00", detalhe)
        # Parcial: o Arroz sem saldo, os demais com saldo. A tela diz que as somas de estoque são parciais; a
        # lista, não: o Arroz vendeu no último relatório e não entraria no parado com saldo nenhum.
        parcial = self.registrar(NOV24, DEZ24, NOV25,
                                 _relatorio(2025, 12, ["1;Café 500g;4;20,00;80,00;60,00;24", "3;Leite 1L;5;5,00;25,00;20,00;68",
                                                       "4;Arroz 5kg;2;27,90;55,80;44,00;"]),
                                 contato={**CONTATO, "email": "parcial@example.invalid"})
        html = self.client.get(f"{PAGINA}/{parcial.id}").get_data(as_text=True)
        self.assertIn("1 produto(s) com custo e sem estoque informado ficam fora das parcelas de estoque", html)
        gravado = json.loads(parcial.resumo_inteligencia_json)
        self.assertEqual((gravado["itens_sem_estoque_informado"], gravado["itens_sem_estoque_com_custo"],
                          gravado["itens_parados_sem_estoque_informado"]), (1, 1, 0))
        lista = self.client.get(PAGINA).get_data(as_text=True)
        self.assertIn("parado há 60 dias ou mais", lista)
        self.assertNotIn("dias ou mais (parcial)", lista)
        # O Feijão, sem saldo e sem venda em 2025 (61 dias cobertos), falta na soma do parado: a lista marca
        # a soma desse lead como parcial, e só a dele.
        feijao = "5;Feijao 1kg;3;8,00;24,00;18,00;"
        com_feijao = self.registrar(
            _relatorio(2024, 11, ["1;Cafe 500g;10;20,00;200,00;150,00;30", "2;Acucar 1kg;5;6,00;30,00;;12",
                                  "3;Leite 1L;20;5,00;100,00;110,00;80", feijao]),
            _relatorio(2024, 12, ["1;Cafe 500g;10;20,00;200,00;150,00;28", "2;Acucar 1kg;5;6,00;30,00;;10",
                                  "3;Leite 1L;20;5,00;100,00;90,00;75", feijao]),
            NOV25, DEZ25, contato={**CONTATO, "email": "feijao@example.invalid"})
        gravado = json.loads(com_feijao.resumo_inteligencia_json)
        self.assertEqual((gravado["itens_sem_estoque_informado"], gravado["itens_sem_estoque_com_custo"],
                          gravado["itens_parados_sem_estoque_informado"]), (1, 1, 1))
        self.assertEqual(self.client.get(PAGINA).get_data(as_text=True).count("dias ou mais (parcial)"), 1)
        # O Açúcar, sem saldo, mas também sem custo e sem venda em 2025: ficaria fora das somas de estoque com
        # saldo ou sem. Nem a lista marca a soma como parcial, nem a tela avisa.
        sem_custo = self.registrar(
            _relatorio(2024, 11, ["1;Cafe 500g;10;20,00;200,00;150,00;30", "2;Acucar 1kg;5;6,00;30,00;;",
                                  "3;Leite 1L;20;5,00;100,00;110,00;80"]),
            _relatorio(2024, 12, ["1;Cafe 500g;10;20,00;200,00;150,00;28", "2;Acucar 1kg;5;6,00;30,00;;",
                                  "3;Leite 1L;20;5,00;100,00;90,00;75"]),
            NOV25, DEZ25, contato={**CONTATO, "email": "acucar@example.invalid"})
        gravado = json.loads(sem_custo.resumo_inteligencia_json)
        self.assertEqual((gravado["itens_sem_estoque_informado"], gravado["itens_sem_estoque_com_custo"],
                          gravado["itens_parados_sem_estoque_informado"]), (1, 0, 0))
        self.assertNotIn("sem estoque informado ficam fora das parcelas",
                         self.client.get(f"{PAGINA}/{sem_custo.id}").get_data(as_text=True))
        self.assertEqual(self.client.get(PAGINA).get_data(as_text=True).count("dias ou mais (parcial)"), 1)

    def test_com_o_calculo_atual_o_caixa_nao_usa_o_resumo_gravado(self):
        # Coluna de estoque vazia e um resumo gravado da versão atual que diz outra coisa (zeros e produtos sem
        # saldo): com as linhas na retenção, só o cálculo atual vale, no JSON e na tela.
        import inteligencia_estoque
        lead = self.registrar(*VAZIO)
        lead.resumo_inteligencia_json = json.dumps({
            "versao": inteligencia_estoque.VERSAO_INTELIGENCIA, "com_estoque": True, "capital_em_estoque": 0.0,
            "capital_parado": {"60": 0.0, "90": 0.0, "180": 0.0}, "itens_parados": {"60": 0, "90": 0, "180": 0},
            "capital_excedente_liberavel": 0.0, "itens_sem_estoque_informado": 3, "itens_sem_estoque_com_custo": 2,
            "itens_parados_sem_estoque_informado": 2, "urgencia_receita_a_preco_medio": 0.0,
            "urgencia_capital_ao_custo": 0.0, "urgencia_itens_com_estoque": 0, "diferenca_de_preco_adicional": 0.0,
            "itens_abaixo_do_preco_de_referencia": 0})
        self.db.session.commit()
        self.login("admin-test@example.invalid")
        payload = self.client.get(f"{PAGINA}/{lead.id}/diagnostico.json").get_json()
        self.assertEqual(payload["situacao"], "atual")
        self.assertIsNotNone(payload["resumo_inteligencia"])
        caixa = payload["visualizacoes"]["caixa_resgatavel"]
        self.assertEqual(caixa["series"][0]["valores"][1:], [None, None, None])
        self.assertEqual(caixa["rotulos"][1], "Estoque parado")
        html = self.client.get(f"{PAGINA}/{lead.id}").get_data(as_text=True)
        self.assertNotIn("sem estoque informado ficam fora das parcelas", html)
        self.assertNotIn("Valores do resumo gravado no envio de", html)
        # Sem as linhas, o resumo gravado volta a valer: é o que resta do lead.
        self.db.session.execute(text("DELETE FROM brainstorming_dados_pdv"))
        self.db.session.commit()
        with patch("blueprints.leads.expurgo_oportunista"):
            expirado = self.client.get(f"{PAGINA}/{lead.id}/diagnostico.json").get_json()
            html_expirado = self.client.get(f"{PAGINA}/{lead.id}").get_data(as_text=True)
        self.assertEqual(expirado["visualizacoes"]["caixa_resgatavel"]["series"][0]["valores"][1:], [0.0, 0.0, 0.0])
        # A nota de soma parcial conta, no gravado, só os produtos sem saldo e com custo (2, não os 3 sem saldo).
        self.assertIn("2 produto(s) com custo e sem estoque informado ficam fora das parcelas de estoque", html_expirado)
        self.assertIn("Valores do resumo gravado no envio de", html_expirado)

    def test_falha_da_inteligencia_usa_o_resumo_gravado_e_diz_isso(self):
        # Linhas na retenção, mas a inteligência não pôde ser calculada: o caixa sai do resumo gravado, e a tela
        # diz que os valores são os do envio.
        lead = self.registrar()
        gravado = json.loads(lead.resumo_inteligencia_json)
        self.login("admin-test@example.invalid")
        with patch("inteligencia_estoque.calcular", side_effect=ValueError("ilegível")):
            payload = self.client.get(f"{PAGINA}/{lead.id}/diagnostico.json").get_json()
            html = self.client.get(f"{PAGINA}/{lead.id}").get_data(as_text=True)
        self.assertEqual((payload["situacao"], payload["inteligencia"]), ("atual", None))
        self.assertEqual(payload["visualizacoes"]["caixa_resgatavel"]["series"][0]["valores"][2],
                         gravado["capital_excedente_liberavel"])
        self.assertIn("Valores do resumo gravado no envio de", html)

    def test_empate_exato_com_o_custo_na_tela(self):
        # Custo igual à venda no Leite (Urgência): sem desconto, o caixa empata com o custo (equilíbrio 0,0%), sem
        # ficar abaixo dele.
        nov25 = _relatorio(2025, 11, ["1;Café 500g;8;20,00;150,00;120,00;26", "3;Leite 1L;5;5,00;25,00;25,00;70"])
        dez25 = _relatorio(2025, 12, ["1;Café 500g;4;20,00;80,00;60,00;24", "3;Leite 1L;5;5,00;25,00;25,00;68"])
        lead = self.registrar(NOV24, DEZ24, nov25, dez25)
        self.login("admin-test@example.invalid")
        simulador = self.client.get(f"{PAGINA}/{lead.id}/diagnostico.json").get_json()["inteligencia"]["simulador"]
        self.assertEqual((simulador["desconto_de_equilibrio"], simulador["abaixo_do_custo_sem_desconto"]), (0.0, False))
        html = self.client.get(f"{PAGINA}/{lead.id}").get_data(as_text=True)
        self.assertIn("o caixa empata com o custo com 0,0% de desconto", html)
        self.assertNotIn("mesmo sem desconto o caixa fica abaixo do custo", html)

    def test_resumo_sem_uma_das_contagens_nao_marca_a_lista(self):
        # Um resumo da versão atual sem a contagem dos produtos sem saldo com custo (o formato de um código
        # intermediário, nunca publicado) vale nada: a lista não mostra o parado nem a marca de parcial dele.
        import inteligencia_estoque
        lead = self.registrar()
        lead.resumo_inteligencia_json = json.dumps({
            "versao": inteligencia_estoque.VERSAO_INTELIGENCIA, "com_estoque": True, "capital_em_estoque": 830.0,
            "capital_parado": {"60": 0.0, "90": 0.0, "180": 0.0}, "itens_parados": {"60": 0, "90": 0, "180": 0},
            "capital_excedente_liberavel": 570.33, "itens_sem_estoque_informado": 1,
            "itens_parados_sem_estoque_informado": 1, "urgencia_receita_a_preco_medio": 340.0,
            "urgencia_capital_ao_custo": 272.0, "urgencia_itens_com_estoque": 1, "diferenca_de_preco_adicional": 0.0,
            "itens_abaixo_do_preco_de_referencia": 0})
        self.db.session.commit()
        self.login("admin-test@example.invalid")
        lista = self.client.get(PAGINA).get_data(as_text=True)
        self.assertNotIn("(parcial)", lista)
        self.assertNotIn("parado há", lista)

    def test_tela_de_detalhe_registra_a_leitura(self):
        lead = self.registrar()
        self.login("admin-test@example.invalid")
        self.stack.enter_context(patch.object(self.app.logger, "disabled", False))
        with self.assertLogs(self.app.logger, "INFO") as registro:
            self.client.get(f"{PAGINA}/{lead.id}")
            self.client.get(f"{PAGINA}/{lead.id}")
        linhas = [linha for linha in registro.output if "lido pelo consultor" in linha]
        self.assertEqual(len(linhas), 2)
        for linha in linhas:
            self.assertIn(str(lead.id), linha)
            for dado in (*CONTATO.values(), "Leite"):
                self.assertNotIn(str(dado), linha)

    def test_resumo_gravado_mostra_a_data_do_envio_e_nao_diz_hoje(self):
        lead = self.registrar()
        self.db.session.execute(text("DELETE FROM brainstorming_dados_pdv"))
        self.db.session.commit()
        self.login("admin-test@example.invalid")
        with patch("blueprints.leads.expurgo_oportunista"):
            html = self.client.get(f"{PAGINA}/{lead.id}").get_data(as_text=True)
        self.assertIn("Valores do resumo gravado no envio de", html)
        self.assertIn("pelo último saldo informado nos relatórios (não é inventário)", html)
        self.assertNotIn("hoje em estoque", html)

    def test_mensagens_do_simulador_com_o_exemplo_do_campo(self):
        lead = self.registrar()
        self.login("admin-test@example.invalid")
        descontos = self.client.get(f"{PAGINA}/{lead.id}?descontos=10%25").get_data(as_text=True)
        self.assertIn("Descontos: valor decimal ilegível; use frações com ponto separadas por vírgula (0.1,0.25)", descontos)
        fracao = self.client.get(f"{PAGINA}/{lead.id}?fracao_liquidada=0").get_data(as_text=True)
        self.assertIn("Fração do estoque vendida: maior que 0 e até 1", fracao)
        self.assertIn("Maior que 0 e até 1, com ponto ou vírgula (0,5 = metade).", fracao)

    def test_simulador_aceita_virgula_decimal_e_avisa_com_o_nome_do_campo(self):
        lead = self.registrar()
        self.login("admin-test@example.invalid")
        html = self.client.get(f"{PAGINA}/{lead.id}?descontos=0,1;0,25&fracao_liquidada=0,5").get_data(as_text=True)
        self.assertNotIn("Parâmetros não aplicados.", html)
        self.assertIn("25,0%", html)
        self.assertIn("50,0%", html)
        self.assertNotIn('name="descontos" maxlength="60" inputmode="decimal"', html)
        payload = self.client.get(f"{PAGINA}/{lead.id}/diagnostico.json?markup_informado=0,35").get_json()
        self.assertEqual(payload["inteligencia"]["auditoria_de_preco"]["markup_informado"], 0.35)
        erro = self.client.get(f"{PAGINA}/{lead.id}?cobertura_alvo_dias=abc").get_data(as_text=True)
        self.assertIn("Cobertura-alvo (dias): um número inteiro de 1 a 3650.", erro)
        self.assertNotIn("cobertura_alvo_dias ilegível", erro)

    def test_barras_do_resumo_executivo_sao_decorativas(self):
        lead = self.registrar()
        self.login("admin-test@example.invalid")
        html = self.client.get(f"{PAGINA}/{lead.id}").get_data(as_text=True)
        self.assertIn('<div class="progress mt-1" style="height: .5rem" aria-hidden="true">', html)
        self.assertNotIn('role="progressbar"', html)

    def test_resumo_gravado_com_numero_gigante_nao_derruba_as_telas(self):
        lead = self.registrar()
        import inteligencia_estoque
        gigante = "1" + "0" * 400
        lead.resumo_inteligencia_json = ('{"versao": "%s", "com_estoque": true, "capital_em_estoque": %s}'
                                         % (inteligencia_estoque.VERSAO_INTELIGENCIA, gigante))
        lead.resumo_urgencia_json = '{"itens": 1, "faturamento": %s, "recuperavel": "0", "motivos": {}}' % gigante
        # Resumo de bloco sem o faturamento: vale nada (antes, a tela caía no Jinja).
        lead.resumo_atencao_json = '{"itens": 1, "recuperavel": "0", "motivos": {}}'
        self.db.session.commit()
        self.login("admin-test@example.invalid")
        with patch("blueprints.leads.expurgo_oportunista"):
            for caminho in (PAGINA, f"{PAGINA}/{lead.id}", f"{PAGINA}/{lead.id}/diagnostico.json"):
                with self.subTest(caminho=caminho):
                    self.assertEqual(self.client.get(caminho).status_code, 200)

    def test_lista_mostra_triagem_marcas_e_caixa(self):
        lead = self.registrar()
        self.login("admin-test@example.invalid")
        html = self.client.get(PAGINA).get_data(as_text=True)
        for texto in ("<th>Triagem</th>", "<th>Marcas</th>", "Caixa resgatável", 'title="Urgência"', "sem custo",
                      "abaixo do custo", "com omissão no ERP", "recuperável", "parado há 60 dias ou mais"):
            self.assertIn(texto, html)
        resumo = json.loads(lead.resumo_urgencia_json)
        self.assertIn(f'<abbr title="Urgência">U</abbr> {resumo["itens"]}', html)
