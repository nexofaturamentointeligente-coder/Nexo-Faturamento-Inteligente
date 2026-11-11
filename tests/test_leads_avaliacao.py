"""Avaliação de Cenário (revisão f4c2a8e1d9b6): tabelas de leads, restrições e expurgo de 30 dias.

Em SQLite, com as migrações reais (tests/deploy_support.py). O que depende do PostgreSQL
(RLS, políticas da Data API, privilégios e a função expurgar_brainstorming_pdv) está em
tests/test_leads_postgresql.py. Os dados são fictícios.
"""

import contextlib
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import io
import uuid

from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from tests.deploy_support import ROOT, DeployTestCase

AGORA = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
# Última revisão das tabelas de lead (a f4c2a8e1d9b6 as criou; 5e1b9c3a7d20 ampliou a staging,
# 6a8f2d4c0e13 agenda o expurgo no pg_cron, sem efeito no SQLite, 7c4e2a9b5d31 é a Etapa 2,
# 9b3e5d7f1a28, a Etapa 3, e c5f8a1d3e7b9, os indicadores executivos do lead).
REVISAO = "c5f8a1d3e7b9"
REVISAO_ETAPA_3 = "9b3e5d7f1a28"
REVISAO_ETAPA_2 = "7c4e2a9b5d31"
REVISAO_SEM_EFEITO = "6a8f2d4c0e13"
REVISAO_QUE_RECUSA = "5e1b9c3a7d20"
REVISAO_ANTERIOR = "b3e9d6c1f7a2"


class _LeadsCase(DeployTestCase):
    def lead(self, status="concluido", **campos):
        from models import LeadAvaliacao
        dados = {"nome_contato": "Contato fictício", "email": f"lead-{uuid.uuid4().hex[:8]}@example.invalid",
                 "whatsapp": "11900000000", "nome_loja": "Loja fictícia", "status": status,
                 "criado_em": AGORA - timedelta(days=60), "atualizado_em": AGORA - timedelta(days=60)}
        dados.update(campos)
        lead = LeadAvaliacao(**dados)
        self.db.session.add(lead)
        self.db.session.flush()
        return lead

    def linha(self, lead, dias_atras, **campos):
        from models import BrainstormingDadosPdv
        dados = {"lead_id": lead.id, "ano_exercicio": 2026, "codigo_produto": "P1",
                 "chave_produto": uuid.uuid4().hex[:16], "descricao_produto": "Produto fictício",
                 "quantidade": Decimal("3"), "valor_venda_bruto": Decimal("30.00"),
                 "criado_em": AGORA - timedelta(days=dias_atras)}
        dados.update(campos)
        linha = BrainstormingDadosPdv(**dados)
        self.db.session.add(linha)
        self.db.session.flush()
        return linha

    def contar(self, modelo, *filtros):
        from sqlalchemy import func
        return self.db.session.scalar(select(func.count()).select_from(modelo).where(*filtros))


class EsquemaTests(_LeadsCase):
    def test_tabelas_colunas_e_indices_da_revisao(self):
        inspetor = inspect(self.db.engine)
        self.assertEqual(self.db.session.scalar(text("SELECT version_num FROM alembic_version")), REVISAO)
        colunas = {c["name"] for c in inspetor.get_columns("lead_avaliacao")}
        self.assertEqual(colunas, {
            "id", "nome_contato", "email", "whatsapp", "nome_loja", "score_saude_cadastral", "vazamento_total_r$",
            "status", "plano_recomendado_codigo", "resumo_urgencia_json", "resumo_atencao_json",
            "resumo_eletiva_json", "criado_em", "atualizado_em",
            # 5e1b9c3a7d20
            "versao_regras", "versao_triagem", "periodo_inicio", "periodo_fim", "arquivos", "meta_json",
            "itens_venda_sem_custo", "itens_venda_abaixo_custo", "registrado_por", "consentimento_em",
            # 7c4e2a9b5d31
            "itens_omissao_erp", "limiares_triagem_json",
            # 9b3e5d7f1a28
            "resumo_inteligencia_json",
            # c5f8a1d3e7b9
            "indicadores_executivos_json"})
        colunas = {c["name"] for c in inspetor.get_columns("brainstorming_dados_pdv")}
        self.assertEqual(colunas, {
            "id", "lead_id", "ano_exercicio", "codigo_produto", "descricao_produto", "quantidade",
            "valor_venda_bruto", "custo_medio_registrado", "flag_venda_sem_custo", "flag_venda_abaixo_custo",
            "flag_omissao_erp", "vazamento_estimado_r$", "criado_em",
            # 5e1b9c3a7d20
            "chave_produto", "quantidade_com_custo", "faturamento_com_custo", "custo_total", "perda",
            "vazamento_preco", "recuperavel", "linhas", "meses", "variacao", "conflito_cadastral",
            "estoque_negativo", "bloco_triagem", "versao_regras",
            # 7c4e2a9b5d31
            "faturamento_em_relatorio_com_custo", "meses_com_custo", "meses_margem_negativa", "ultimo_mes_negativo",
            "meses_completos", "quantidade_mesmos_meses", "estoque_final", "categoria_produto", "ncm_ausente",
            "categoria_ausente", "custo_bonificacoes", "custo_devolucoes", "devolucoes_com_custo",
            "relatorios_desde_primeira_venda",
            # 9b3e5d7f1a28
            "fim_ultimo_relatorio_com_venda"})
        [fk] = inspetor.get_foreign_keys("brainstorming_dados_pdv")
        self.assertEqual((fk["name"], fk["referred_table"], fk["options"].get("ondelete")),
                         ("fk_brainstorming_dados_pdv_lead", "lead_avaliacao", "CASCADE"))
        fks = {fk["referred_table"]: fk for fk in inspetor.get_foreign_keys("lead_avaliacao")}
        self.assertEqual(set(fks), {"plano_nivel", "usuario"})
        self.assertEqual(fks["plano_nivel"]["referred_columns"], ["codigo"])
        self.assertEqual((fks["usuario"]["referred_columns"], fks["usuario"]["options"].get("ondelete")),
                         (["id_usuario"], "SET NULL"))
        indices = {i["name"] for t in ("lead_avaliacao", "brainstorming_dados_pdv") for i in inspetor.get_indexes(t)}
        self.assertTrue({"ix_lead_avaliacao_plano", "ix_lead_avaliacao_status", "ix_lead_avaliacao_email",
                         "ix_lead_avaliacao_registrado_por", "ix_brainstorming_dados_pdv_lead",
                         "ix_brainstorming_dados_pdv_criado_em"} <= indices)
        unicos = {u["name"]: u["column_names"] for u in inspetor.get_unique_constraints("brainstorming_dados_pdv")}
        self.assertEqual(unicos.get("uq_brainstorming_dados_pdv_lead_ano_produto"),
                         ["lead_id", "ano_exercicio", "chave_produto"])

    def test_valores_com_cifrao_no_nome_da_coluna_e_padroes(self):
        lead = self.lead(status="pendente", vazamento_total_reais=Decimal("1234.56"),
                         score_saude_cadastral=Decimal("72.50"), plano_recomendado_codigo="PRATA")
        linha = self.linha(lead, 1, vazamento_estimado_reais=Decimal("4.50"), custo_medio_registrado=Decimal("12.3456"))
        self.db.session.commit()
        bruto = self.db.session.execute(text(
            'SELECT "vazamento_total_r$", score_saude_cadastral FROM lead_avaliacao')).one()
        self.assertEqual((Decimal(str(bruto[0])), Decimal(str(bruto[1]))), (Decimal("1234.56"), Decimal("72.50")))
        self.db.session.expire_all()
        self.assertIsInstance(lead.id, uuid.UUID)
        self.assertEqual((linha.flag_venda_sem_custo, linha.flag_venda_abaixo_custo, linha.flag_omissao_erp),
                         (False, False, False))
        self.assertEqual(linha.vazamento_estimado_reais, Decimal("4.50"))
        self.assertEqual(linha.custo_medio_registrado, Decimal("12.3456"))
        self.assertEqual(linha.criado_em.tzinfo, timezone.utc)
        # Os padrões do banco valem também para quem grava fora da aplicação.
        self.db.session.execute(text(
            "INSERT INTO lead_avaliacao (id, nome_contato, email, nome_loja) "
            "VALUES ('00000000000000000000000000000001', 'Outro', 'outro@example.invalid', 'Loja')"))
        status, criado = self.db.session.execute(text(
            "SELECT status, criado_em FROM lead_avaliacao WHERE nome_contato = 'Outro'")).one()
        self.assertEqual(status, "pendente")
        self.assertIsNotNone(criado)

    def test_restricoes_recusam_dado_invalido(self):
        invalidos = {
            "status": {"status": "aprovado"},
            "score acima de 100": {"score_saude_cadastral": Decimal("100.01")},
            "score negativo": {"score_saude_cadastral": Decimal("-1")},
            "vazamento negativo": {"vazamento_total_reais": Decimal("-0.01")},
            "e-mail sem arroba": {"email": "sem-arroba.example.invalid"},
            "nome vazio": {"nome_contato": "   "},
            "loja vazia": {"nome_loja": ""},
            "whatsapp curto": {"whatsapp": "123"},
            "plano inexistente": {"plano_recomendado_codigo": "DIAMANTE"},
            # 5e1b9c3a7d20
            "nenhum relatório": {"arquivos": 0},
            "itens sem custo negativos": {"itens_venda_sem_custo": -1},
            "itens abaixo do custo negativos": {"itens_venda_abaixo_custo": -1},
            "período invertido": {"periodo_inicio": date(2025, 2, 1), "periodo_fim": date(2025, 1, 31)},
            "consultor inexistente": {"registrado_por": 987654321},
        }
        for caso, campos in invalidos.items():
            with self.subTest(caso=caso):
                with self.assertRaises(IntegrityError):
                    self.lead(**campos)
                self.db.session.rollback()
        lead = self.lead()
        # Gravada antes do laço: cada caso desfaz o que fez, e esta linha precisa continuar lá.
        repetida = self.linha(lead, 1, chave_produto="a1b2c3d4e5f60718")
        self.db.session.commit()
        for caso, campos in {"ano": {"ano_exercicio": 1999}, "descrição vazia": {"descricao_produto": " "},
                             "custo negativo": {"custo_medio_registrado": Decimal("-1")},
                             "vazamento negativo": {"vazamento_estimado_reais": Decimal("-1")},
                             "lead inexistente": {"lead_id": uuid.uuid4()},
                             # 5e1b9c3a7d20
                             "chave vazia": {"chave_produto": ""},
                             "soma negativa": {"perda": Decimal("-0.01")},
                             "meses negativos": {"meses": -1},
                             "bloco desconhecido": {"bloco_triagem": "imediata"},
                             "produto repetido no ano": {"chave_produto": repetida.chave_produto,
                                                         "lead_id": repetida.lead_id}}.items():
            with self.subTest(caso=caso):
                with self.assertRaises(IntegrityError):
                    self.linha(lead, 1, **campos)
                self.db.session.rollback()
                lead = self.lead()

    def test_excluir_o_lead_leva_os_dados_do_pdv(self):
        from models import BrainstormingDadosPdv, LeadAvaliacao
        lead, outro = self.lead(), self.lead()
        for dias in (1, 2, 3):
            self.linha(lead, dias)
        self.linha(outro, 1)
        self.db.session.commit()
        self.db.session.execute(text("DELETE FROM lead_avaliacao WHERE id = :id"), {"id": lead.id.hex})
        self.db.session.commit()
        self.assertEqual(self.contar(BrainstormingDadosPdv), 1)
        self.assertEqual(self.contar(LeadAvaliacao), 1)
        self.db.session.delete(outro)
        self.db.session.commit()
        self.assertEqual(self.contar(BrainstormingDadosPdv), 0)


class ExpurgoTests(_LeadsCase):
    def _cenario(self):
        self.antigo = self.lead("concluido")
        for _ in range(3):
            self.linha(self.antigo, 31)
        self.convertido = self.lead("convertido")
        self.linha(self.convertido, 90)
        self.misto = self.lead("processamento_manual")
        self.linha(self.misto, 45)
        self.linha(self.misto, 5)
        self.recente = self.lead("pendente")
        self.linha(self.recente, 29)
        self.limite = self.lead("em_processamento")
        self.linha(self.limite, 30)  # exatamente 30 dias: fica
        self.expirado = self.lead("expirado")
        self.linha(self.expirado, 40)
        self.sem_linhas = self.lead("pendente")
        self.db.session.commit()

    def _status(self):
        self.db.session.expire_all()
        return {nome: getattr(self, nome).status
                for nome in ("antigo", "convertido", "misto", "recente", "limite", "expirado", "sem_linhas")}

    def test_regra_do_expurgo(self):
        import leads_avaliacao
        from models import BrainstormingDadosPdv, LeadAvaliacao
        self._cenario()
        resumo_antes = self.db.session.execute(select(
            LeadAvaliacao.id, LeadAvaliacao.score_saude_cadastral, LeadAvaliacao.vazamento_total_reais)).all()
        resultado = leads_avaliacao.expurgar_dados_pdv(self.db.session, agora=AGORA)
        self.db.session.commit()
        self.assertEqual(resultado, leads_avaliacao.ResultadoExpurgo(leads_expirados=1, linhas_removidas=5))
        self.assertEqual(self._status(), {"antigo": "expirado", "convertido": "convertido",
                                          "misto": "processamento_manual", "recente": "pendente",
                                          "limite": "em_processamento", "expirado": "expirado",
                                          "sem_linhas": "pendente"})
        restantes = {lead_id: n for lead_id, n in self.db.session.execute(text(
            "SELECT lead_id, count(*) FROM brainstorming_dados_pdv GROUP BY lead_id"))}
        self.assertEqual(restantes, {self.convertido.id.hex: 1, self.misto.id.hex: 1,
                                     self.recente.id.hex: 1, self.limite.id.hex: 1})
        self.assertEqual(self.contar(BrainstormingDadosPdv, BrainstormingDadosPdv.lead_id == self.antigo.id), 0)
        # O resumo do lead fica.
        self.assertEqual(self.db.session.execute(select(
            LeadAvaliacao.id, LeadAvaliacao.score_saude_cadastral, LeadAvaliacao.vazamento_total_reais)).all(),
            resumo_antes)
        self.assertEqual(self.antigo.atualizado_em, AGORA)
        # Repetir não faz nada.
        self.assertEqual(leads_avaliacao.expurgar_dados_pdv(self.db.session, agora=AGORA),
                         leads_avaliacao.ResultadoExpurgo(0, 0))

    def test_prazo_configuravel_e_validado(self):
        import leads_avaliacao
        self._cenario()
        resultado = leads_avaliacao.expurgar_dados_pdv(self.db.session, 60, agora=AGORA)
        self.assertEqual(resultado, leads_avaliacao.ResultadoExpurgo(0, 0))
        resultado = leads_avaliacao.expurgar_dados_pdv(self.db.session, 1, agora=AGORA)
        self.assertEqual(resultado, leads_avaliacao.ResultadoExpurgo(leads_expirados=4, linhas_removidas=8))
        self.db.session.rollback()
        for dias in (0, -1, None, 1.5, "30", True):
            with self.subTest(dias=dias):
                with self.assertRaises(ValueError):
                    leads_avaliacao.expurgar_dados_pdv(self.db.session, dias, agora=AGORA)

    def test_comando_simula_por_padrao_e_aplica_com_a_opcao(self):
        from models import BrainstormingDadosPdv
        from unittest.mock import patch
        import leads_avaliacao
        self._cenario()
        self.db.session.remove()
        original = leads_avaliacao.expurgar_dados_pdv

        def no_relogio_do_teste(sessao, dias=30):
            return original(sessao, dias, agora=AGORA)

        executor = self.app.test_cli_runner()
        with patch.object(leads_avaliacao, "expurgar_dados_pdv", side_effect=no_relogio_do_teste):
            simulado = executor.invoke(args=["leads", "expurgar-pdv"])
            self.assertEqual(simulado.exit_code, 0, simulado.output)
            self.assertIn("SIMULAÇÃO (nada foi gravado)", simulado.output)
            self.assertIn("a remover: 5; leads a expirar: 1", simulado.output)
            self.assertEqual(self.contar(BrainstormingDadosPdv), 9)
            aplicado = executor.invoke(args=["leads", "expurgar-pdv", "--aplicar"])
            self.assertEqual(aplicado.exit_code, 0, aplicado.output)
            self.assertIn("removidas: 5; leads expirados: 1", aplicado.output)
        self.assertEqual(self.contar(BrainstormingDadosPdv), 4)
        self.assertNotIn("example.invalid", simulado.output + aplicado.output)
        self.assertNotIn("Contato fictício", simulado.output + aplicado.output)
        recusado = executor.invoke(args=["leads", "expurgar-pdv", "--dias", "0"])
        self.assertNotEqual(recusado.exit_code, 0)


class DowngradeTests(_LeadsCase):
    def _migrar(self, comando, revisao):
        from unittest.mock import patch
        from flask_migrate import downgrade, upgrade
        funcao = {"downgrade": downgrade, "upgrade": upgrade}[comando]
        self.db.session.remove()
        # Sem o fileConfig() do Alembic, que trocaria os handlers e desligaria os loggers.
        with patch("logging.config.fileConfig"), contextlib.redirect_stdout(io.StringIO()),                 contextlib.redirect_stderr(io.StringIO()):
            funcao(directory=str(ROOT / "migrations"), revision=revisao)

    def test_downgrade_da_etapa_2_recusa_dados_das_regras_novas(self):
        lead = self.lead(itens_omissao_erp=2)
        self.linha(lead, 1, versao_regras="lead-2026-11.3", categoria_produto="BEBIDAS")
        self.db.session.commit()
        with self.assertLogs("flask_migrate", "ERROR") as registro, self.assertRaises(SystemExit):
            self._migrar("downgrade", REVISAO_SEM_EFEITO)
        self.assertIn("Downgrade da Etapa 2 da Avaliação de Cenário recusado", registro.output[0])
        # Sem dado da Etapa 3 nem indicadores, a c5f8a1d3e7b9 e a 9b3e5d7f1a28 descem; o SQLite registra esses
        # passos antes da recusa.
        self.assertIn(self.db.session.scalar(text("SELECT version_num FROM alembic_version")),
                      (REVISAO, REVISAO_ETAPA_3, REVISAO_ETAPA_2))
        self.assertEqual(self.db.session.scalar(text("SELECT categoria_produto FROM brainstorming_dados_pdv")),
                         "BEBIDAS")

    def _recusa_da_etapa_3(self):
        with self.assertLogs("flask_migrate", "ERROR") as registro, self.assertRaises(SystemExit):
            self._migrar("downgrade", REVISAO_ETAPA_2)
        self.assertIn("Downgrade da Etapa 3 da Avaliação de Cenário recusado", registro.output[0])
        # Sem indicadores no lead, a c5f8a1d3e7b9 desce antes; o SQLite pode registrar esse passo antes da recusa.
        self.assertIn(self.db.session.scalar(text("SELECT version_num FROM alembic_version")),
                      (REVISAO, REVISAO_ETAPA_3))

    def test_downgrade_da_etapa_3_recusa_linha_das_regras_novas_ou_resumo_preenchido(self):
        lead = self.lead()
        self.linha(lead, 1, versao_regras="lead-2026-11.4", fim_ultimo_relatorio_com_venda=date(2026, 10, 31))
        self.db.session.commit()
        # (a) Linha das regras lead-2026-11.4.
        self._recusa_da_etapa_3()
        self.assertEqual(self.db.session.scalar(text(
            "SELECT fim_ultimo_relatorio_com_venda FROM brainstorming_dados_pdv")), "2026-10-31")
        # (b) Linha de outra versão com a data preenchida.
        self.db.session.execute(text("UPDATE brainstorming_dados_pdv SET versao_regras = 'lead-2026-11.3'"))
        self.db.session.commit()
        self._recusa_da_etapa_3()
        # (c) Só o resumo da inteligência no lead.
        self.db.session.execute(text("UPDATE brainstorming_dados_pdv SET fim_ultimo_relatorio_com_venda = NULL"))
        self.db.session.execute(text("UPDATE lead_avaliacao SET resumo_inteligencia_json = '{}'"))
        self.db.session.commit()
        self._recusa_da_etapa_3()
        # Sem nada da Etapa 3: desce sem perder linha de PDV nem lead (o lead não é recriado, e o CASCADE
        # não age) e sobe de novo com a coluna nula.
        self.db.session.execute(text("UPDATE lead_avaliacao SET resumo_inteligencia_json = NULL"))
        self.db.session.commit()
        self._migrar("downgrade", REVISAO_ETAPA_2)
        self.assertEqual(self.db.session.scalar(text("SELECT version_num FROM alembic_version")), REVISAO_ETAPA_2)
        inspetor = inspect(self.db.engine)
        self.assertNotIn("fim_ultimo_relatorio_com_venda",
                         {c["name"] for c in inspetor.get_columns("brainstorming_dados_pdv")})
        self.assertNotIn("resumo_inteligencia_json", {c["name"] for c in inspetor.get_columns("lead_avaliacao")})
        self.assertEqual(self.db.session.scalar(text("SELECT count(*) FROM brainstorming_dados_pdv")), 1)
        self.assertEqual(self.db.session.scalar(text("SELECT count(*) FROM lead_avaliacao")), 1)
        self._migrar("upgrade", "head")
        self.assertEqual(self.db.session.scalar(text("SELECT version_num FROM alembic_version")), REVISAO)
        self.assertEqual(self.db.session.scalar(text("SELECT count(*) FROM brainstorming_dados_pdv")), 1)
        self.assertIsNone(self.db.session.scalar(text(
            "SELECT fim_ultimo_relatorio_com_venda FROM brainstorming_dados_pdv")))

    def test_data_da_ultima_venda_fora_da_faixa_e_recusada(self):
        lead = self.lead()
        self.linha(lead, 1, fim_ultimo_relatorio_com_venda=date(2000, 1, 1))
        self.db.session.commit()
        with self.assertRaises(IntegrityError):
            self.linha(lead, 1, fim_ultimo_relatorio_com_venda=date(1999, 12, 31))
        self.db.session.rollback()
        with self.assertRaises(IntegrityError):
            self.linha(lead, 1, fim_ultimo_relatorio_com_venda=date(2101, 1, 1))
        self.db.session.rollback()

    def test_upgrade_da_etapa_2_nao_apaga_linhas_de_pdv(self):
        # Volta à 6a8f2d4c0e13 sem dado da Etapa 2, grava uma linha e sobe de novo: a linha fica.
        self._migrar("downgrade", REVISAO_SEM_EFEITO)
        self.db.session.execute(text(
            "INSERT INTO lead_avaliacao (id, nome_contato, email, nome_loja, status, criado_em, atualizado_em) "
            "VALUES ('00000000000000000000000000000001', 'Contato', 'lead@example.invalid', 'Loja', 'concluido', "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
        self.db.session.execute(text(
            "INSERT INTO brainstorming_dados_pdv (id, lead_id, ano_exercicio, chave_produto, descricao_produto, "
            "quantidade, valor_venda_bruto, criado_em) VALUES ('00000000000000000000000000000002', "
            "'00000000000000000000000000000001', 2026, 'abcdef0123456789', 'Produto', 1, 10, CURRENT_TIMESTAMP)"))
        self.db.session.commit()
        self._migrar("upgrade", "head")
        self.assertEqual(self.db.session.scalar(text("SELECT count(*) FROM brainstorming_dados_pdv")), 1)
        self.assertEqual(self.db.session.scalar(text(
            "SELECT meses_com_custo FROM brainstorming_dados_pdv")), 0)

    def test_downgrade_recusa_dados_e_remove_as_tabelas_vazias(self):
        lead = self.lead()
        self.linha(lead, 1)
        self.db.session.commit()
        # O Flask-Migrate registra o motivo e sai com código 1.
        with self.assertLogs("flask_migrate", "ERROR") as registro, self.assertRaises(SystemExit) as saida:
            self._migrar("downgrade", REVISAO_ANTERIOR)
        self.assertEqual(saida.exception.code, 1)
        self.assertIn("Downgrade da Avaliação de Cenário recusado", registro.output[0])
        # O SQLite não tem DDL transacional no Alembic: os passos da 7c4e2a9b5d31 (sem linha da Etapa 2, ela
        # desce) e da 6a8f2d4c0e13 (sem efeito) podem ficar registrados antes da recusa da 5e1b9c3a7d20. No
        # PostgreSQL a recusa desfaz tudo (ensaio próprio). A linha de PDV fica: a 7c4e2a9b5d31 não recria
        # a tabela do lead, e o CASCADE não a apaga.
        self.assertIn(self.db.session.scalar(text("SELECT version_num FROM alembic_version")),
                      (REVISAO, REVISAO_ETAPA_2, REVISAO_SEM_EFEITO, REVISAO_QUE_RECUSA))
        self.assertEqual(self.db.session.scalar(text("SELECT count(*) FROM brainstorming_dados_pdv")), 1)
        colunas = {c["name"] for c in inspect(self.db.engine).get_columns("brainstorming_dados_pdv")}
        self.assertIn("chave_produto", colunas)
        self.db.session.execute(text("DELETE FROM lead_avaliacao"))
        self.db.session.commit()
        self._migrar("downgrade", REVISAO_ANTERIOR)
        tabelas = set(inspect(self.db.engine).get_table_names())
        self.assertFalse({"lead_avaliacao", "brainstorming_dados_pdv"} & tabelas)
        self.assertIn("plano_nivel", tabelas)
        self._migrar("upgrade", "head")
        self.assertTrue({"lead_avaliacao", "brainstorming_dados_pdv"} <= set(inspect(self.db.engine).get_table_names()))
