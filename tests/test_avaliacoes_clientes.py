"""COM-002: avaliações dos clientes, do "Deixar Avaliação" da landing ao carrossel.

Executar da raiz: python -B -m unittest tests.test_avaliacoes_clientes -v
SQLite isolado de tests/deploy_support.py, migrações reais e dados fictícios (example.invalid). Opcionais:
  - PostgreSQL real com NEXO_TEST_POSTGRES_BIN: cluster temporário num processo próprio, como em
    tests/test_planos_postgresql.py (RLS, políticas, privilégios, criação e adoção pela d3f5b7a9c1e2, divergências
    recusadas, downgrade, envios simultâneos e exclusão definitiva);
  - navegador real com NEXO_TEST_BROWSER=1 (Edge ou Chromium pelo Playwright, CDN só das cópias locais de
    NEXO_TEST_CDN_DIR): o modal por teclado, foco preso e devolvido, Esc e 320 px sem rolagem horizontal.

Cobre:
  1. contexto_da_landing em cada estado (visitante, cliente que pode avaliar, empresa suspensa, termos a aceitar,
     admin, quem já avaliou) e falhando aberto (banco fora, tabelas ausentes ou parciais), com só o tipo do erro
     no log;
  2. o parcial templates/_avaliacao_landing.html em cada estado (render_template_string com include);
  3. /avaliar e o login com next=/avaliar pelo formulário real (destino externo continua recusado);
  4. o envio pelo modal (JSON e nunca flash): 200 com a avaliação PENDENTE, os eventos e o aviso aos admins;
     422 por campo; 400 sem CSRF; 409 na segunda (também na corrida); 403 da empresa suspensa; 429 no 11º envio;
  5. moderação: aprovar publica só com autorização; rejeitar exige motivo; retirar tira do carrossel; decisão
     sobre versão antiga recusada; edição e retirada da autorização pelo cliente; o DTO público mínimo e
     escapado na landing;
  6. a revisão d3f5b7a9c1e2 em SQLite: downgrade recusado com dados e aceito vazio; adoção das tabelas criadas
     pelo SQL da c5e8a1f4b7d2 (linhas preservadas, índice ausente criado); divergências e esquema parcial
     interrompem sem mudar nada; prévia offline;
  7. a exclusão permanente de empresa com avaliação, sugestão, eventos, pedido dos 60 dias e aviso aos admins.
"""

import contextlib
from datetime import datetime, timezone
from decimal import Decimal
import io
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from flask import render_template, render_template_string, url_for
from flask_login import AnonymousUserMixin
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import OperationalError

from tests.deploy_support import DeployTestCase, ROOT
# Guarda as funções de socket de antes do bloqueio de rede do setUp (o servidor local do navegador as usa).
from tests.test_nexobot_navegador import _CONECTAR_LOCAL

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # dependência opcional de desenvolvimento
    sync_playwright = None


REVISAO = "d3f5b7a9c1e2"
REVISAO_ANTERIOR = "c8e2a4f6b0d1"
TABELAS = ("avaliacao_cliente", "sugestao_melhoria", "evento_avaliacao", "solicitacao_avaliacao")
COMENTARIO = "A NEXO organizou nossos relatórios e ajudou a decidir as compras do mês."
FETCH = {"X-Requested-With": "fetch", "Accept": "application/json"}
CHAVES_DO_DTO = {"approved", "publication_consent", "quote", "author", "company", "nota", "data", "data_iso"}

# O SQL que a revisão c5e8a1f4b7d2 (02/11/2026, fora da cadeia) emite no PostgreSQL, tirado da prévia offline dela
# (flask db upgrade --sql), só com os espaços do fim das linhas aparados: é a estrutura que o banco do .env já tem.
# No SQLite a prévia dela é a mesma com as três trocas do dialeto de sql_c5e8 (conferidas contra a prévia).
SQL_C5E8_POSTGRESQL = (
    """CREATE TABLE avaliacao_cliente (
    id_avaliacao SERIAL NOT NULL,
    id_usuario INTEGER NOT NULL,
    id_empresa INTEGER NOT NULL,
    nota INTEGER NOT NULL,
    comentario_publico TEXT NOT NULL,
    nome_exibicao VARCHAR(60),
    autoriza_publicacao BOOLEAN DEFAULT false NOT NULL,
    autoriza_nome_empresa BOOLEAN DEFAULT false NOT NULL,
    consentimento_versao VARCHAR(20),
    consentimento_em TIMESTAMP WITH TIME ZONE,
    consentimento_retirado_em TIMESTAMP WITH TIME ZONE,
    status_moderacao VARCHAR(20) NOT NULL,
    motivo_rejeicao TEXT,
    moderada_em TIMESTAMP WITH TIME ZONE,
    id_usuario_moderador INTEGER,
    publicada_em TIMESTAMP WITH TIME ZONE,
    retirada_em TIMESTAMP WITH TIME ZONE,
    retirada_motivo TEXT,
    versao INTEGER DEFAULT '1' NOT NULL,
    data_criacao TIMESTAMP WITH TIME ZONE NOT NULL,
    data_atualizacao TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (id_avaliacao),
    FOREIGN KEY(id_usuario) REFERENCES usuario (id_usuario),
    FOREIGN KEY(id_empresa) REFERENCES empresa (id_empresa),
    FOREIGN KEY(id_usuario_moderador) REFERENCES usuario (id_usuario),
    CONSTRAINT uq_avaliacao_cliente_usuario UNIQUE (id_usuario),
    CONSTRAINT ck_avaliacao_nota CHECK (nota BETWEEN 1 AND 5),
    CONSTRAINT ck_avaliacao_comentario CHECK (length(trim(comentario_publico)) > 0 AND length(comentario_publico) <= 1000),
    CONSTRAINT ck_avaliacao_status CHECK (status_moderacao IN ('PENDENTE', 'APROVADA', 'REJEITADA')),
    CONSTRAINT ck_avaliacao_consentimento CHECK (autoriza_publicacao = false OR (consentimento_em IS NOT NULL AND consentimento_versao IS NOT NULL AND consentimento_retirado_em IS NULL)),
    CONSTRAINT ck_avaliacao_nome_empresa CHECK (autoriza_nome_empresa = false OR autoriza_publicacao = true),
    CONSTRAINT ck_avaliacao_moderacao CHECK (status_moderacao = 'PENDENTE' OR (moderada_em IS NOT NULL AND id_usuario_moderador IS NOT NULL)),
    CONSTRAINT ck_avaliacao_rejeicao CHECK (status_moderacao <> 'REJEITADA' OR motivo_rejeicao IS NOT NULL),
    CONSTRAINT ck_avaliacao_publicacao CHECK (publicada_em IS NULL OR retirada_em IS NOT NULL OR (status_moderacao = 'APROVADA' AND autoriza_publicacao = true)),
    CONSTRAINT ck_avaliacao_retirada CHECK (retirada_em IS NULL OR publicada_em IS NOT NULL),
    CONSTRAINT ck_avaliacao_versao CHECK (versao >= 1)
)""",
    "CREATE INDEX ix_avaliacao_cliente_empresa ON avaliacao_cliente (id_empresa)",
    "CREATE INDEX ix_avaliacao_cliente_moderacao ON avaliacao_cliente (status_moderacao, data_atualizacao)",
    "CREATE INDEX ix_avaliacao_cliente_publicacao ON avaliacao_cliente (status_moderacao, autoriza_publicacao, publicada_em)",
    """CREATE TABLE sugestao_melhoria (
    id_sugestao SERIAL NOT NULL,
    id_avaliacao INTEGER NOT NULL,
    texto TEXT NOT NULL,
    status_acompanhamento VARCHAR(20) NOT NULL,
    data_criacao TIMESTAMP WITH TIME ZONE NOT NULL,
    data_atualizacao TIMESTAMP WITH TIME ZONE NOT NULL,
    status_alterado_em TIMESTAMP WITH TIME ZONE,
    id_usuario_admin_status INTEGER,
    PRIMARY KEY (id_sugestao),
    FOREIGN KEY(id_avaliacao) REFERENCES avaliacao_cliente (id_avaliacao) ON DELETE CASCADE,
    FOREIGN KEY(id_usuario_admin_status) REFERENCES usuario (id_usuario),
    CONSTRAINT uq_sugestao_avaliacao UNIQUE (id_avaliacao),
    CONSTRAINT ck_sugestao_texto CHECK (length(trim(texto)) > 0 AND length(texto) <= 2000),
    CONSTRAINT ck_sugestao_status CHECK (status_acompanhamento IN ('NOVA', 'EM_ANALISE', 'CONCLUIDA'))
)""",
    "CREATE INDEX ix_sugestao_status ON sugestao_melhoria (status_acompanhamento, data_atualizacao)",
    """CREATE TABLE evento_avaliacao (
    id_evento SERIAL NOT NULL,
    id_avaliacao INTEGER NOT NULL,
    tipo VARCHAR(40) NOT NULL,
    ocorrido_em TIMESTAMP WITH TIME ZONE NOT NULL,
    id_usuario_responsavel INTEGER NOT NULL,
    justificativa TEXT,
    consentimento_versao VARCHAR(20),
    PRIMARY KEY (id_evento),
    FOREIGN KEY(id_avaliacao) REFERENCES avaliacao_cliente (id_avaliacao) ON DELETE CASCADE,
    FOREIGN KEY(id_usuario_responsavel) REFERENCES usuario (id_usuario),
    CONSTRAINT ck_evento_avaliacao_tipo CHECK (tipo IN ('CRIADA', 'EDITADA', 'APROVADA', 'REJEITADA', 'PUBLICADA', 'RETIRADA', 'CONSENTIMENTO_CONCEDIDO', 'CONSENTIMENTO_RETIRADO', 'SUGESTAO_ATUALIZADA'))
)""",
    "CREATE INDEX ix_evento_avaliacao ON evento_avaliacao (id_avaliacao, ocorrido_em)",
    """CREATE TABLE solicitacao_avaliacao (
    id_usuario INTEGER NOT NULL,
    ultima_exibicao_em TIMESTAMP WITH TIME ZONE,
    adiada_em TIMESTAMP WITH TIME ZONE,
    proxima_exibicao_em TIMESTAMP WITH TIME ZONE,
    qtd_adiamentos INTEGER DEFAULT '0' NOT NULL,
    PRIMARY KEY (id_usuario),
    FOREIGN KEY(id_usuario) REFERENCES usuario (id_usuario) ON DELETE CASCADE,
    CONSTRAINT ck_solicitacao_adiamentos CHECK (qtd_adiamentos >= 0)
)""",
)

# Divergências que interrompem a adoção: (troca no SQL da c5e8a1f4b7d2, trecho esperado na mensagem). Valem nos
# dois bancos; a do tipo do instante só no PostgreSQL (no SQLite o fuso não fica na coluna).
DIVERGENCIAS = (
    (("comentario_publico TEXT NOT NULL", "comentario_publico VARCHAR(500) NOT NULL"),
     "avaliacao_cliente.comentario_publico: tipo VARCHAR(500)"),
    (("nome_exibicao VARCHAR(60),", "nome_exibicao VARCHAR(60) NOT NULL,"),
     "avaliacao_cliente.nome_exibicao: recusa nulo"),
    ((",\n    CONSTRAINT ck_avaliacao_versao CHECK (versao >= 1)", ""),
     "avaliacao_cliente: falta o CHECK ck_avaliacao_versao"),
    (("REFERENCES avaliacao_cliente (id_avaliacao) ON DELETE CASCADE,\n    FOREIGN KEY(id_usuario_admin_status)",
      "REFERENCES avaliacao_cliente (id_avaliacao),\n    FOREIGN KEY(id_usuario_admin_status)"),
     "sugestao_melhoria: falta a chave estrangeira (id_avaliacao) -> avaliacao_cliente(id_avaliacao) ON DELETE CASCADE"),
    (("    justificativa TEXT,\n", "    justificativa TEXT,\n    observacao TEXT,\n"),
     "evento_avaliacao.observacao: coluna que models.py não tem"),
    (("qtd_adiamentos INTEGER DEFAULT '0' NOT NULL", "qtd_adiamentos INTEGER DEFAULT '5' NOT NULL"),
     "solicitacao_avaliacao.qtd_adiamentos: padrão do servidor"),
    (("ON evento_avaliacao (id_avaliacao, ocorrido_em)", "ON evento_avaliacao (ocorrido_em)"),
     "evento_avaliacao: índice ix_evento_avaliacao"),
    (("    CONSTRAINT uq_sugestao_avaliacao UNIQUE (id_avaliacao),\n", ""),
     "sugestao_melhoria: restrições únicas"),
)


def sql_c5e8(dialeto, troca=None, *, sem=()):
    """Os comandos da c5e8a1f4b7d2 no dialeto, com uma troca (que precisa casar uma vez) e sem os que contêm
    algum dos trechos de `sem`."""
    comandos = list(SQL_C5E8_POSTGRESQL)
    if dialeto == "sqlite":
        comandos = [comando.replace("SERIAL", "INTEGER").replace("DEFAULT false", "DEFAULT 0")
                    .replace("TIMESTAMP WITH TIME ZONE", "DATETIME") for comando in comandos]
    if troca is not None:
        antes, depois = troca
        if sum(comando.count(antes) for comando in comandos) != 1:
            raise AssertionError(f"A troca precisa casar exatamente uma vez: {antes!r}")
        comandos = [comando.replace(antes, depois) for comando in comandos]
    return [comando for comando in comandos if not any(trecho in comando for trecho in sem)]


class _AvaliacoesCase(DeployTestCase):
    def setUp(self):
        super().setUp()
        import avaliacoes
        self.servico = avaliacoes
        avaliacoes._esquema_por_engine.clear()
        self.addCleanup(avaliacoes._esquema_por_engine.clear)
        self.fixtures()
        self.empresa_id = self.usuario(self.cliente_id).id_empresa

    # ------------------------------------------------------------------ apoio
    def usuario(self, id_usuario):
        from models import Usuario
        return self.db.session.get(Usuario, id_usuario)

    def avaliacoes(self):
        from models import AvaliacaoCliente
        self.db.session.expire_all()
        return self.db.session.scalars(select(AvaliacaoCliente).order_by(AvaliacaoCliente.id_avaliacao)).all()

    def eventos(self, id_avaliacao):
        from models import EventoAvaliacao
        return [(evento.tipo, evento.consentimento_versao) for evento in self.db.session.scalars(
            select(EventoAvaliacao).where(EventoAvaliacao.id_avaliacao == id_avaliacao)
            .order_by(EventoAvaliacao.id_evento))]

    def novo_cliente(self, email="cliente2-test@example.invalid", nome_fantasia="Teste 2"):
        from models import Empresa, Usuario
        empresa = self.db.session.scalar(select(Empresa).where(Empresa.nome_fantasia == nome_fantasia))
        usuario = Usuario(nome="Outro cliente fictício", email=email, role="CLIENTE",
                          id_empresa=empresa.id_empresa, primeiro_acesso=False)
        usuario.set_senha(self.password)
        self.db.session.add(usuario)
        self.db.session.commit()
        return usuario.id_usuario

    def suspender(self, id_empresa=None):
        from models import Empresa
        self.db.session.get(Empresa, id_empresa or self.empresa_id).status_conta = "SUSPENSA"
        self.db.session.commit()

    def entrar(self, email="cliente-test@example.invalid"):
        """Login pelo formulário; devolve o token CSRF da sessão."""
        self.login(email)
        return self.csrf_token("/admin/dashboard" if email.startswith("admin") else "/cliente/dashboard")

    def enviar(self, token, *, nota="5", comentario=COMENTARIO, autoriza=True, json=True, **extra):
        dados = {"csrf_token": token, "nota": nota, "comentario": comentario, **extra}
        if autoriza:
            dados["autoriza_publicacao"] = "1"
        return self.client.post("/cliente/avaliacao", data=dados, headers=FETCH if json else {})

    def criar_direto(self, id_usuario=None, *, autoriza=True, nota=5, comentario=COMENTARIO, sugestao=None):
        """Avaliação gravada pelo serviço (sem a rota) e confirmada; devolve o id."""
        dados = self.servico.DadosAvaliacao(nota, comentario, sugestao, autoriza, None, False)
        avaliacao = self.servico.criar_avaliacao(self.db.session, self.usuario(id_usuario or self.cliente_id), dados)
        self.db.session.commit()
        return avaliacao.id_avaliacao

    def aprovar_direto(self, id_avaliacao):
        from models import AvaliacaoCliente
        versao = self.db.session.get(AvaliacaoCliente, id_avaliacao).versao
        self.servico.aprovar_avaliacao(self.db.session, id_avaliacao, self.usuario(self.admin_id), versao)
        self.db.session.commit()

    def politica_publica(self):
        """/politica-de-privacidade quando a página pública dos documentos legais está registrada (frente J);
        None antes dela. O modal usa a página sozinho, sem configuração."""
        if "documentos_publicos.politica_de_privacidade" not in self.app.view_functions:
            return None
        with self.app.test_request_context("/"):
            return url_for("documentos_publicos.politica_de_privacidade")

    def sem_flash(self):
        with self.client.session_transaction() as sessao:
            return "_flashes" not in sessao

    def contexto(self, usuario):
        with self.app.test_request_context("/"):
            return self.servico.contexto_da_landing(usuario)

    def parcial(self, contexto):
        with self.app.test_request_context("/"):
            return render_template_string('{% include "_avaliacao_landing.html" %}', **contexto)

    def publicar_termos(self):
        """Uma versão vigente dos Termos de Uso, ainda sem o aceite do cliente."""
        from models import DocumentoLegal
        documento = DocumentoLegal(tipo="TERMOS_USO", versao=1, versao_maior=1, versao_menor=0,
                                   conteudo="Termos fictícios de teste.", publicado_em=datetime.now(timezone.utc),
                                   id_usuario_admin=self.admin_id)
        self.db.session.add(documento)
        self.db.session.commit()
        return documento.id_documento


# =============================================================================
# 1. contexto_da_landing
# =============================================================================
class ContextoDaLandingTests(_AvaliacoesCase):
    def test_visitante_ve_o_caminho_do_login_sem_formulario(self):
        contexto = self.contexto(AnonymousUserMixin())
        self.assertEqual(contexto["avaliacao_estado"], "visitante")
        self.assertIsNone(contexto["avaliacao_motivo"])
        self.assertIsNone(contexto["avaliacao_formulario"])
        self.assertEqual((contexto["avaliar_url"], contexto["minha_avaliacao_url"], contexto["moderacao_url"]),
                         ("/avaliar", "/cliente/minha-avaliacao", "/admin/avaliacoes"))
        self.assertEqual(contexto["landing_reviews"], [])
        self.assertEqual(self.contexto(None)["avaliacao_estado"], "visitante")

    def test_cliente_ativo_pode_avaliar_com_os_dados_do_modal(self):
        contexto = self.contexto(self.usuario(self.cliente_id))
        self.assertEqual(contexto["avaliacao_estado"], "pode_avaliar")
        self.assertEqual(contexto["avaliacao_formulario"], {
            "acao_url": "/cliente/avaliacao", "rotulos_nota": self.servico.ROTULOS_NOTA,
            "comentario_min": 5, "comentario_max": 1000,
            "texto_consentimento": self.servico.TEXTO_CONSENTIMENTO, "politica_url": self.politica_publica(),
        })
        self.assertIsNone(contexto["avaliacao_continuar_url"])

    def test_empresa_suspensa_fica_inelegivel_com_o_motivo(self):
        self.suspender()
        contexto = self.contexto(self.usuario(self.cliente_id))
        self.assertEqual(contexto["avaliacao_estado"], "inelegivel")
        self.assertIn("empresas com conta ativa", contexto["avaliacao_motivo"])
        self.assertIsNone(contexto["avaliacao_formulario"])

    def test_termos_a_aceitar_levam_ao_aceite_antes_do_modal(self):
        from models import AceiteDocumento
        id_documento = self.publicar_termos()
        contexto = self.contexto(self.usuario(self.cliente_id))
        self.assertEqual((contexto["avaliacao_estado"], contexto["avaliacao_motivo"], contexto["avaliacao_continuar_url"]),
                         ("inelegivel", self.servico.MOTIVO_TERMOS_PENDENTES, "/avaliar"))
        self.db.session.add(AceiteDocumento(id_usuario=self.cliente_id, id_documento=id_documento,
                                            aceito_em=datetime.now(timezone.utc)))
        self.db.session.commit()
        self.assertEqual(self.contexto(self.usuario(self.cliente_id))["avaliacao_estado"], "pode_avaliar")

    def test_admin_ve_o_caminho_da_moderacao(self):
        contexto = self.contexto(self.usuario(self.admin_id))
        self.assertEqual((contexto["avaliacao_estado"], contexto["avaliacao_formulario"]), ("admin", None))

    def test_quem_ja_avaliou_ve_a_propria_avaliacao_mesmo_suspenso(self):
        self.criar_direto()
        self.assertEqual(self.contexto(self.usuario(self.cliente_id))["avaliacao_estado"], "ja_avaliou")
        self.suspender()
        self.assertEqual(self.contexto(self.usuario(self.cliente_id))["avaliacao_estado"], "ja_avaliou")

    def test_depoimentos_aprovados_vao_para_todos_os_estados(self):
        id_avaliacao = self.criar_direto(nota=4)
        self.aprovar_direto(id_avaliacao)
        for usuario in (AnonymousUserMixin(), self.usuario(self.admin_id), self.usuario(self.cliente_id)):
            with self.subTest(usuario=type(usuario).__name__):
                depoimentos = self.contexto(usuario)["landing_reviews"]
                self.assertEqual(len(depoimentos), 1)
                self.assertEqual(set(depoimentos[0]), CHAVES_DO_DTO)
                self.assertEqual((depoimentos[0]["nota"], depoimentos[0]["author"], depoimentos[0]["company"]),
                                 (4, "Cliente NEXO", ""))
                self.assertRegex(depoimentos[0]["data"], r"^\d{2}/\d{2}/\d{4}$")

    def _falha_aberta(self, alvo, erro):
        with patch.object(self.app.logger, "disabled", False), \
                self.assertLogs(self.app.logger, "WARNING") as registro, patch(alvo, side_effect=erro):
            contexto = self.contexto(self.usuario(self.cliente_id))
        self.assertEqual((contexto["avaliacao_estado"], contexto["landing_reviews"], contexto["avaliacao_formulario"]),
                         ("indisponivel", [], None))
        saida = "\n".join(registro.output)
        self.assertIn(type(erro).__name__, saida)
        self.assertNotIn("segredo-do-banco", saida)
        return contexto

    def test_banco_fora_falha_aberto_com_so_o_tipo_do_erro_no_log(self):
        erro = OperationalError("SELECT 1", {}, Exception("conexão recusada: segredo-do-banco"))
        # Na conferência do esquema e na consulta dos depoimentos.
        self._falha_aberta("avaliacoes.inspect", erro)
        self.assertTrue(self.servico.esquema_avaliacoes_disponivel())  # confirmada de novo depois da falha
        self._falha_aberta("avaliacoes.depoimentos_publicos", erro)
        # A presença confirmada deixa de valer depois de uma falha: a próxima reconfere o banco.
        self.assertNotIn(self.db.engine, self.servico._esquema_por_engine)
        # E com a sessão revertida a página segue: a consulta seguinte funciona.
        self.assertEqual(self.contexto(self.usuario(self.cliente_id))["avaliacao_estado"], "pode_avaliar")

    def test_tabelas_ausentes_ou_parciais_deixam_a_secao_indisponivel(self):
        with patch("avaliacoes.esquema_avaliacoes_disponivel", return_value=False):
            self.assertEqual(self.contexto(self.usuario(self.cliente_id))["avaliacao_estado"], "indisponivel")
        parcial = self.servico.EsquemaAvaliacoesInconsistente("parcial: segredo-do-banco")
        self._falha_aberta("avaliacoes.esquema_avaliacoes_disponivel", parcial)

    def test_politica_publicada_entra_no_modal(self):
        with self.app.test_request_context("/"):
            self.assertEqual(self.servico.url_politica_privacidade(), self.politica_publica())
        self.app.config["POLITICA_PRIVACIDADE_URL"] = "/politica-de-privacidade"
        self.addCleanup(self.app.config.pop, "POLITICA_PRIVACIDADE_URL", None)
        self.assertEqual(self.contexto(self.usuario(self.cliente_id))["avaliacao_formulario"]["politica_url"],
                         "/politica-de-privacidade")
        for invalida in ("//outro.example", "javascript:alert(1)", "/\\outro.example"):
            self.app.config["POLITICA_PRIVACIDADE_URL"] = invalida
            with self.subTest(url=invalida), self.app.test_request_context("/"):
                # Endereço recusado: vale a página pública, se houver, ou nada.
                self.assertEqual(self.servico.url_politica_privacidade(), self.politica_publica())

    def test_pagina_publica_dos_documentos_vira_o_link_da_politica(self):
        if self.politica_publica() is None:  # antes da junção com a frente J, uma rota com o mesmo endpoint
            self.app.add_url_rule("/politica-de-privacidade", endpoint="documentos_publicos.politica_de_privacidade",
                                  view_func=lambda: "Política fictícia")
        self.assertEqual(self.contexto(self.usuario(self.cliente_id))["avaliacao_formulario"]["politica_url"],
                         self.politica_publica())
        self.assertIsNotNone(self.politica_publica())


# =============================================================================
# 2. O parcial da landing em cada estado
# =============================================================================
class ParcialDaLandingTests(_AvaliacoesCase):
    def assert_sem_formulario(self, html):
        self.assertNotIn("<dialog", html)
        self.assertNotIn("<form", html)
        self.assertNotIn("csrf_token", html)

    def test_visitante_tem_link_para_avaliar_que_funciona_sem_javascript(self):
        html = self.parcial(self.contexto(AnonymousUserMixin()))
        self.assertRegex(html, r'<a class="btn-nexo btn-nexo-secondary" href="/avaliar"[^>]*>Deixar Avaliação</a>')
        self.assertIn('data-avaliacao-estado="visitante"', html)
        self.assert_sem_formulario(html)

    def test_cliente_que_pode_avaliar_recebe_um_unico_dialogo_com_o_formulario(self):
        html = self.parcial(self.contexto(self.usuario(self.cliente_id)))
        self.assertEqual(html.count("<dialog"), 1)
        self.assertEqual(html.count('id="avaliacao-modal"'), 1)
        self.assertEqual(html.count("<form"), 1)
        self.assertRegex(html, r'<form method="POST" action="/cliente/avaliacao"')
        # O botão é um link de reserva para o formulário do portal (sem JavaScript).
        self.assertRegex(html, r'<a class="btn-nexo btn-nexo-secondary" href="/cliente/avaliacao" data-avaliacao-abrir')
        notas = re.findall(r'<input type="radio" id="avaliacao-nota-(\d)" name="nota" value="(\d)" required>', html)
        self.assertEqual(notas, [(str(n), str(n)) for n in range(1, 6)])
        for n, rotulo in self.servico.ROTULOS_NOTA.items():
            self.assertIn(f"{n} {'estrela' if n == 1 else 'estrelas'}: {rotulo}", html)
        self.assertRegex(html, r'<textarea id="avaliacao-comentario" name="comentario"[^>]*minlength="5" maxlength="1000"')
        caixa = re.search(r'<input type="checkbox" id="avaliacao-autoriza" name="autoriza_publicacao"[^>]*>', html)
        self.assertIsNotNone(caixa)
        self.assertNotIn("checked", caixa.group(0))
        self.assertRegex(html, r'<input type="hidden" name="csrf_token" value="[^"]{20,}">')
        politica = self.politica_publica()
        if politica is None:
            self.assertIn(self.servico.TEXTO_CONSENTIMENTO, html)
            self.assertIn("A Política de Privacidade ainda não foi publicada", html)
        else:
            antes, depois = self.servico.TEXTO_CONSENTIMENTO.split("Política de Privacidade")
            self.assertIn(f'{antes}<a href="{politica}" target="_blank" rel="noopener noreferrer">'
                          f'Política de Privacidade</a>{depois}', html)
            self.assertNotIn("ainda não foi publicada", html)
        self.assertIn('data-minha-avaliacao-url="/cliente/minha-avaliacao"', html)
        self.assertIn('data-avaliar-url="/avaliar"', html)
        # Nenhum script inline nem manipulador on*: o modal é todo do arquivo estático (CSP).
        self.assertNotIn("<script", html)
        self.assertIsNone(re.search(r"\son[a-z]+=", html))

    def test_com_a_politica_publicada_o_aviso_tem_o_link(self):
        self.app.config["POLITICA_PRIVACIDADE_URL"] = "/politica-de-privacidade"
        self.addCleanup(self.app.config.pop, "POLITICA_PRIVACIDADE_URL", None)
        html = self.parcial(self.contexto(self.usuario(self.cliente_id)))
        self.assertIn('<a href="/politica-de-privacidade" target="_blank" rel="noopener noreferrer">'
                      'Política de Privacidade</a>', html)
        self.assertNotIn("ainda não foi publicada", html)

    def test_quem_ja_avaliou_ve_o_link_da_propria_avaliacao(self):
        self.criar_direto()
        html = self.parcial(self.contexto(self.usuario(self.cliente_id)))
        self.assertRegex(html, r'href="/cliente/minha-avaliacao"[^>]*>Ver minha avaliação</a>')
        self.assert_sem_formulario(html)

    def test_inelegivel_ve_o_motivo_e_com_termos_o_caminho_do_aceite(self):
        self.suspender()
        html = self.parcial(self.contexto(self.usuario(self.cliente_id)))
        self.assertIn("empresas com conta ativa", html)
        self.assertNotIn("Aceitar os termos", html)
        self.assert_sem_formulario(html)
        from models import Empresa
        self.db.session.get(Empresa, self.empresa_id).status_conta = "ATIVA"
        self.db.session.commit()
        self.publicar_termos()
        html = self.parcial(self.contexto(self.usuario(self.cliente_id)))
        self.assertIn(self.servico.MOTIVO_TERMOS_PENDENTES, html)
        self.assertRegex(html, r'href="/avaliar"[^>]*>Aceitar os termos e avaliar</a>')
        self.assert_sem_formulario(html)

    def test_admin_ve_a_moderacao(self):
        html = self.parcial(self.contexto(self.usuario(self.admin_id)))
        self.assertRegex(html, r'href="/admin/avaliacoes"[^>]*>Ver as avaliações recebidas</a>')
        self.assert_sem_formulario(html)

    def test_indisponivel_mostra_o_botao_desabilitado_e_o_aviso(self):
        with patch("avaliacoes.esquema_avaliacoes_disponivel", return_value=False):
            html = self.parcial(self.contexto(self.usuario(self.cliente_id)))
        self.assertRegex(html, r'<button type="button" class="btn-nexo btn-nexo-secondary" disabled '
                               r'aria-describedby="reviews-availability">Deixar Avaliação</button>')
        self.assertIn('id="reviews-availability"', html)
        self.assert_sem_formulario(html)
        # Sem contexto nenhum (a landing incluiu o parcial sem as chaves), o padrão é o mesmo estado.
        self.assertIn("disabled", self.parcial({}))

    def test_texto_do_contexto_sai_escapado(self):
        html = self.parcial({"avaliacao_estado": "inelegivel", "avaliacao_motivo": '<img src=x onerror="alert(1)">',
                             "avaliacao_continuar_url": None})
        self.assertIn("&lt;img src=x onerror=", html)
        self.assertNotIn("<img", html)


# =============================================================================
# 3. /avaliar e o login com next
# =============================================================================
class AvaliarELoginTests(_AvaliacoesCase):
    def test_visitante_vai_ao_login_e_o_formulario_preserva_o_next(self):
        resposta = self.client.get("/avaliar")
        self.assertEqual((resposta.status_code, resposta.headers["Location"]), (302, "/auth/login?next=/avaliar"))
        pagina = self.client.get(resposta.headers["Location"]).get_data(as_text=True)
        acao = re.search(r'<form method="POST" action="([^"]+)"', pagina).group(1)
        self.assertEqual(acao, "/auth/login?next=/avaliar")
        token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', pagina).group(1)
        entrada = self.client.post(acao, data={"email": "cliente-test@example.invalid", "senha": self.password,
                                               "csrf_token": token})
        self.assertEqual((entrada.status_code, entrada.headers["Location"]), (302, "/avaliar"))
        volta = self.client.get("/avaliar")
        self.assertEqual((volta.status_code, volta.headers["Location"]), (302, "/?avaliar=1#avaliacoes"))

    def test_sem_next_o_formulario_fica_como_antes_e_o_erro_de_senha_mantem_o_next(self):
        pagina = self.client.get("/auth/login").get_data(as_text=True)
        self.assertIn('<form method="POST" action="/auth/login">', pagina)
        token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', pagina).group(1)
        errada = self.client.post("/auth/login?next=/avaliar", data={
            "email": "cliente-test@example.invalid", "senha": "senha-errada", "csrf_token": token})
        self.assertEqual(errada.status_code, 200)
        self.assertIn('action="/auth/login?next=/avaliar"', errada.get_data(as_text=True))

    def test_next_externo_continua_recusado(self):
        for destino in ("//outro.example/x", "/\\outro.example", "https://outro.example/", "/\t/outro.example"):
            with self.subTest(destino=destino):
                cliente = self.app.test_client()
                pagina = cliente.get("/auth/login", query_string={"next": destino}).get_data(as_text=True)
                token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', pagina).group(1)
                acao = re.search(r'<form method="POST" action="([^"]+)"', pagina).group(1).replace("&amp;", "&")
                resposta = cliente.post(acao, data={"email": "cliente-test@example.invalid", "senha": self.password,
                                                    "csrf_token": token})
                self.assertEqual((resposta.status_code, resposta.headers["Location"]), (302, "/cliente/dashboard"))

    def test_admin_ja_avaliou_e_suspenso_tem_destinos_proprios(self):
        self.entrar("admin-test@example.invalid")
        self.assertEqual(self.client.get("/avaliar").headers["Location"], "/admin/avaliacoes")
        self.logout()
        self.entrar()
        self.criar_direto()
        self.assertEqual(self.client.get("/avaliar").headers["Location"], "/cliente/minha-avaliacao")
        from models import AvaliacaoCliente
        self.db.session.execute(AvaliacaoCliente.__table__.delete())
        self.db.session.commit()
        self.suspender()
        self.assertEqual(self.client.get("/avaliar").headers["Location"], "/cliente/avaliacao")
        restrito = self.client.get("/cliente/avaliacao")
        self.assertEqual(restrito.status_code, 403)
        self.assertIn("empresas com conta ativa", restrito.get_data(as_text=True))

    def test_termos_a_aceitar_passam_pelo_aceite_e_voltam_ao_modal(self):
        self.publicar_termos()
        self.entrar()
        pagina = self.client.get("/avaliar")
        html = pagina.get_data(as_text=True)
        self.assertEqual(pagina.status_code, 200)
        self.assertIn('id="modalTermos"', html)
        self.assertIn('name="proximo" value="/avaliar"', html)
        aceite = self.client.post("/cliente/termos/aceitar", data={
            "csrf_token": re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html).group(1),
            "proximo": "/avaliar", "confirmo": "1",
            "documento": re.findall(r'name="documento" value="(\d+)"', html)})
        self.assertEqual(aceite.headers["Location"], "/avaliar")
        self.assertEqual(self.client.get("/avaliar").headers["Location"], "/?avaliar=1#avaliacoes")

    def test_sem_as_tabelas_avaliar_avisa_indisponivel(self):
        self.entrar()
        with patch("avaliacoes.esquema_avaliacoes_disponivel", return_value=False):
            resposta = self.client.get("/avaliar")
        self.assertEqual(resposta.status_code, 503)
        self.assertIn("As avaliações estarão disponíveis em breve", resposta.get_data(as_text=True))


# =============================================================================
# 4. Envio pelo modal
# =============================================================================
class EnvioPeloModalTests(_AvaliacoesCase):
    def test_envio_valido_grava_pendente_com_eventos_e_avisa_os_admins_sem_flash(self):
        from models import Notificacao
        token = self.entrar()
        resposta = self.enviar(token, nota="4")
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.get_json(), {"ok": True, "mensagem": self.servico.MENSAGEM_RECEBIDA,
                                               "destino": "/cliente/minha-avaliacao?confirmacao=enviada"})
        self.assertTrue(self.sem_flash())
        [avaliacao] = self.avaliacoes()
        self.assertEqual((avaliacao.nota, avaliacao.status_moderacao, avaliacao.autoriza_publicacao,
                          avaliacao.consentimento_versao, avaliacao.id_empresa, avaliacao.publicada_em),
                         (4, "PENDENTE", True, self.servico.VERSAO_AVISO_CONSENTIMENTO, self.empresa_id, None))
        self.assertEqual(self.eventos(avaliacao.id_avaliacao),
                         [("CRIADA", None), ("CONSENTIMENTO_CONCEDIDO", self.servico.VERSAO_AVISO_CONSENTIMENTO)])
        aviso = self.db.session.scalars(select(Notificacao).where(Notificacao.id_usuario == self.admin_id)).all()
        self.assertEqual([(n.texto, n.link_destino) for n in aviso],
                         [("Nova avaliação de Teste 1 aguardando moderação.",
                           f"/admin/avaliacoes/{avaliacao.id_avaliacao}")])
        # Nada aparece antes da aprovação.
        self.assertEqual(self.servico.depoimentos_publicos(), [])
        # A confirmação do destino vem da lista fechada; outro código não mostra nada.
        self.assertIn(self.servico.MENSAGEM_SUCESSO, self.client.get(resposta.get_json()["destino"]).get_data(as_text=True))
        outro = self.client.get("/cliente/minha-avaliacao?confirmacao=<b>forjado</b>").get_data(as_text=True)
        self.assertNotIn("forjado", outro)
        self.assertNotIn(self.servico.MENSAGEM_SUCESSO, outro)

    def test_sem_autorizacao_a_resposta_diz_que_fica_com_a_equipe(self):
        token = self.entrar()
        resposta = self.enviar(token, autoriza=False)
        self.assertEqual(resposta.get_json()["mensagem"], self.servico.MENSAGEM_RECEBIDA_SEM_PUBLICACAO)
        [avaliacao] = self.avaliacoes()
        self.assertEqual((avaliacao.autoriza_publicacao, avaliacao.consentimento_em), (False, None))
        self.assertEqual(self.eventos(avaliacao.id_avaliacao), [("CRIADA", None)])

    def test_campos_invalidos_dao_422_por_campo_e_nada_e_gravado(self):
        token = self.entrar()
        casos = [({"nota": nota}, "nota", "Selecione uma nota de 1 a 5 estrelas.")
                 for nota in ("0", "6", "3.5", " 4", "٣", "", "cinco")]
        casos += [
            ({"comentario": ""}, "comentario", "Conte como foi sua experiência."),
            ({"comentario": "   \n "}, "comentario", "Conte como foi sua experiência."),
            ({"comentario": "Bom"}, "comentario", "Escreva pelo menos 5 caracteres."),
            ({"comentario": "x" * 1001}, "comentario", "O comentário pode ter até 1000 caracteres."),
            ({"comentario": "x" * 999 + " " * 3200}, "comentario", "O comentário pode ter até 1000 caracteres."),
        ]
        for dados, campo, mensagem in casos:
            with self.subTest(dados=repr(dados)[:60]):
                resposta = self.enviar(token, **dados)
                self.assertEqual(resposta.status_code, 422)
                corpo = resposta.get_json()
                self.assertEqual((corpo["ok"], corpo["mensagem"], corpo["erros"][campo]),
                                 (False, "Confira os campos destacados.", mensagem))
        # Nota repetida no mesmo envio também é recusada.
        repetida = self.client.post("/cliente/avaliacao", headers=FETCH, data={
            "csrf_token": token, "nota": ["4", "5"], "comentario": COMENTARIO})
        self.assertEqual((repetida.status_code, list(repetida.get_json()["erros"])), (422, ["nota"]))
        self.assertEqual(self.avaliacoes(), [])
        self.assertTrue(self.sem_flash())

    def test_sem_csrf_da_400_e_nada_e_gravado(self):
        self.entrar()
        resposta = self.client.post("/cliente/avaliacao", headers=FETCH,
                                    data={"nota": "5", "comentario": COMENTARIO, "autoriza_publicacao": "1"})
        self.assertEqual(resposta.status_code, 400)
        self.assertEqual(self.avaliacoes(), [])

    def test_segunda_avaliacao_da_409_com_o_destino_e_sem_flash(self):
        token = self.entrar()
        self.assertEqual(self.enviar(token).status_code, 200)
        segunda = self.enviar(token, nota="2")
        self.assertEqual(segunda.status_code, 409)
        self.assertEqual(segunda.get_json(), {
            "ok": False, "erros": {}, "mensagem": "Você já enviou sua avaliação. Para consultá-la ou editá-la, "
                                                   "abra Minha avaliação.",
            "destino": "/cliente/minha-avaliacao?confirmacao=registrada"})
        self.assertTrue(self.sem_flash())
        self.assertEqual([a.nota for a in self.avaliacoes()], [5])
        self.assertIn("Sua avaliação já está registrada", self.client.get(segunda.get_json()["destino"]).get_data(as_text=True))

    def test_corrida_entre_dois_envios_vira_409_pela_restricao_unica(self):
        token = self.entrar()
        self.assertEqual(self.enviar(token).status_code, 200)
        # O segundo envio passou pela checagem antes do primeiro gravar: a restrição única decide.
        with patch("avaliacoes.obter_avaliacao_usuario", return_value=None):
            resposta = self.enviar(token, nota="3")
        self.assertEqual(resposta.status_code, 409)
        self.assertEqual(len(self.avaliacoes()), 1)

    def test_sem_javascript_o_post_comum_redireciona_com_flash(self):
        token = self.entrar()
        resposta = self.enviar(token, json=False)
        self.assertEqual((resposta.status_code, resposta.headers["Location"]), (302, "/cliente/minha-avaliacao"))
        self.assertFalse(self.sem_flash())
        self.assertIn(self.servico.MENSAGEM_SUCESSO, self.client.get("/cliente/minha-avaliacao").get_data(as_text=True))

    def test_empresa_suspensa_recebe_403_em_json(self):
        token = self.entrar()
        self.suspender()
        resposta = self.enviar(token)
        self.assertEqual(resposta.status_code, 403)
        self.assertIn("empresas com conta ativa", resposta.get_json()["mensagem"])
        self.assertEqual(self.avaliacoes(), [])

    def test_falha_de_gravacao_da_503_sem_dizer_que_gravou(self):
        token = self.entrar()
        erro = OperationalError("INSERT", {}, Exception("disco cheio: segredo-do-banco"))
        with patch("avaliacoes.criar_avaliacao", side_effect=erro):
            resposta = self.enviar(token)
        self.assertEqual(resposta.status_code, 503)
        self.assertEqual(resposta.get_json()["ok"], False)
        self.assertEqual(self.avaliacoes(), [])

    def test_decimo_primeiro_envio_da_hora_da_429(self):
        from extensions import limiter
        self.app.config["RATELIMIT_ENABLED"] = True
        limiter.init_app(self.app)
        self.addCleanup(limiter.reset)
        token = self.entrar()
        codigos = [self.enviar(token, nota="0").status_code for _ in range(11)]
        self.assertEqual(codigos, [422] * 10 + [429])
        self.assertEqual(self.avaliacoes(), [])

    def test_paginas_do_cliente_renderizam_com_as_macros(self):
        self.criar_direto(sugestao="Sugestão fictícia só para a equipe.")
        self.entrar()
        pagina = self.client.get("/cliente/minha-avaliacao")
        self.assertEqual(pagina.status_code, 200)
        html = pagina.get_data(as_text=True)
        self.assertIn('aria-label="Nota 5 de 5"', html)
        self.assertIn("Sugestão fictícia só para a equipe.", html)
        self.assertEqual(self.client.get("/cliente/avaliacao/editar").status_code, 200)


# =============================================================================
# 5. Moderação e o DTO público
# =============================================================================
class ModeracaoTests(_AvaliacoesCase):
    def decidir(self, acao, id_avaliacao, versao=None, **dados):
        from models import AvaliacaoCliente
        if versao is None:
            self.db.session.expire_all()
            versao = self.db.session.get(AvaliacaoCliente, id_avaliacao).versao
        token = self.csrf_token(f"/admin/avaliacoes/{id_avaliacao}")
        return self.client.post(f"/admin/avaliacoes/{id_avaliacao}/{acao}",
                                data={"csrf_token": token, "versao": str(versao), **dados})

    def test_aprovar_com_autorizacao_publica_e_avisa_o_cliente(self):
        from models import Notificacao
        id_avaliacao = self.criar_direto()
        self.entrar("admin-test@example.invalid")
        detalhe = self.client.get(f"/admin/avaliacoes/{id_avaliacao}")
        self.assertEqual(detalhe.status_code, 200)
        self.assertIn('aria-label="Nota 5 de 5"', detalhe.get_data(as_text=True))
        resposta = self.decidir("aprovar", id_avaliacao)
        self.assertEqual(resposta.headers["Location"], f"/admin/avaliacoes/{id_avaliacao}")
        [avaliacao] = self.avaliacoes()
        self.assertTrue(avaliacao.no_ar)
        self.assertEqual([tipo for tipo, _ in self.eventos(id_avaliacao)][-2:], ["APROVADA", "PUBLICADA"])
        self.assertEqual([d["quote"] for d in self.servico.depoimentos_publicos()], [COMENTARIO])
        aviso = self.db.session.scalars(select(Notificacao.texto).where(Notificacao.id_usuario == self.cliente_id)).all()
        self.assertEqual(aviso, ["Sua avaliação foi aprovada e já aparece nos depoimentos da NEXO."])

    def test_aprovar_sem_autorizacao_nao_publica(self):
        id_avaliacao = self.criar_direto(autoriza=False)
        self.entrar("admin-test@example.invalid")
        self.decidir("aprovar", id_avaliacao)
        [avaliacao] = self.avaliacoes()
        self.assertEqual((avaliacao.status_moderacao, avaliacao.publicada_em), ("APROVADA", None))
        self.assertEqual(self.servico.depoimentos_publicos(), [])

    def test_rejeitar_exige_motivo(self):
        id_avaliacao = self.criar_direto()
        self.entrar("admin-test@example.invalid")
        for motivo in ("", "ok", "  \n "):
            with self.subTest(motivo=motivo):
                self.decidir("rejeitar", id_avaliacao, motivo=motivo)
                self.assertEqual(self.avaliacoes()[0].status_moderacao, "PENDENTE")
        self.decidir("rejeitar", id_avaliacao, motivo="Cita dados de outra empresa.")
        [avaliacao] = self.avaliacoes()
        self.assertEqual((avaliacao.status_moderacao, avaliacao.motivo_rejeicao),
                         ("REJEITADA", "Cita dados de outra empresa."))
        self.assertEqual(self.servico.depoimentos_publicos(), [])

    def test_retirar_tira_do_carrossel_e_versao_antiga_e_recusada(self):
        id_avaliacao = self.criar_direto()
        self.entrar("admin-test@example.invalid")
        versao_lida = self.avaliacoes()[0].versao
        self.decidir("aprovar", id_avaliacao)
        self.assertEqual(len(self.servico.depoimentos_publicos()), 1)
        # Uma decisão sobre a versão de antes da aprovação é recusada e nada muda.
        self.decidir("retirar", id_avaliacao, versao=versao_lida)
        self.assertTrue(self.avaliacoes()[0].no_ar)
        self.decidir("retirar", id_avaliacao, motivo="Pedido da equipe.")
        [avaliacao] = self.avaliacoes()
        self.assertEqual((avaliacao.status_moderacao, avaliacao.no_ar, avaliacao.retirada_motivo),
                         ("APROVADA", False, "Pedido da equipe."))
        self.assertEqual(self.servico.depoimentos_publicos(), [])
        self.assertEqual(self.contexto(AnonymousUserMixin())["landing_reviews"], [])

    def test_edicao_do_cliente_volta_a_moderacao_e_sai_do_ar_sem_flash(self):
        id_avaliacao = self.criar_direto()
        self.aprovar_direto(id_avaliacao)
        token = self.entrar()
        resposta = self.client.post("/cliente/avaliacao/editar", headers=FETCH, data={
            "csrf_token": token, "nota": "3", "comentario": COMENTARIO, "autoriza_publicacao": "1"})
        self.assertEqual(resposta.get_json(), {"ok": True, "destino": "/cliente/minha-avaliacao?confirmacao=reenviada",
                                               "mensagem": "Avaliação atualizada e reenviada para revisão da equipe NEXO."})
        self.assertTrue(self.sem_flash())
        [avaliacao] = self.avaliacoes()
        self.assertEqual((avaliacao.nota, avaliacao.status_moderacao, avaliacao.no_ar), (3, "PENDENTE", False))
        self.assertEqual(self.servico.depoimentos_publicos(), [])

    def test_retirada_da_autorizacao_pelo_cliente_tira_do_ar(self):
        id_avaliacao = self.criar_direto()
        self.aprovar_direto(id_avaliacao)
        token = self.entrar()
        resposta = self.client.post("/cliente/avaliacao/consentimento/retirar", data={"csrf_token": token})
        self.assertEqual(resposta.headers["Location"], "/cliente/minha-avaliacao")
        self.assertEqual(self.servico.depoimentos_publicos(), [])

    def test_telas_do_admin_e_o_item_do_menu(self):
        id_avaliacao = self.criar_direto(sugestao="Melhorar a exportação.")
        self.entrar("admin-test@example.invalid")
        for caminho in ("/admin/avaliacoes", "/admin/avaliacoes?status=PENDENTE", "/admin/avaliacoes/sugestoes",
                        f"/admin/avaliacoes/{id_avaliacao}"):
            with self.subTest(caminho=caminho):
                resposta = self.client.get(caminho)
                self.assertEqual(resposta.status_code, 200)
                html = resposta.get_data(as_text=True)
                self.assertRegex(html, r'class="nav-item active" href="/admin/avaliacoes">\s*<i class="bi bi-star">'
                                       r'</i> Avaliações')
        painel = self.client.get("/admin/dashboard").get_data(as_text=True)
        self.assertRegex(painel, r'class="nav-item " href="/admin/avaliacoes">\s*<i class="bi bi-star"></i> Avaliações')


class DtoPublicoTests(_AvaliacoesCase):
    def test_dto_minimo_e_texto_escapado_na_landing(self):
        malicioso = '<script>alert(1)</script><img src=x onerror="alert(2)"> Excelente atendimento.'
        id_avaliacao = self.criar_direto(comentario=malicioso, sugestao="Sugestão secreta da equipe.")
        self.aprovar_direto(id_avaliacao)
        [dto] = self.servico.depoimentos_publicos()
        self.assertEqual(set(dto), CHAVES_DO_DTO)
        self.assertEqual(dto["quote"], malicioso)
        self.assertNotIn("cliente-test@example.invalid", json.dumps(dto))
        contexto = self.contexto(AnonymousUserMixin())
        with self.app.test_request_context("/"):
            html = render_template("landing.html", **contexto)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn("<script>alert(1)", html)
        self.assertNotIn('<img src=x onerror="alert(2)">', html)
        self.assertNotIn("Sugestão secreta", html)
        self.assertNotIn("cliente-test@example.invalid", html)

    def test_usuario_inativo_ou_empresa_na_lixeira_saem_do_carrossel(self):
        from models import Empresa, Usuario
        id_avaliacao = self.criar_direto()
        self.aprovar_direto(id_avaliacao)
        self.db.session.get(Usuario, self.cliente_id).ativo = False
        self.db.session.commit()
        self.assertEqual(self.servico.depoimentos_publicos(), [])
        self.db.session.get(Usuario, self.cliente_id).ativo = True
        self.db.session.get(Empresa, self.empresa_id).deletado_em = datetime(2026, 11, 1)
        self.db.session.commit()
        self.assertEqual(self.servico.depoimentos_publicos(), [])


# =============================================================================
# 6. A revisão d3f5b7a9c1e2 em SQLite
# =============================================================================
class MigracaoSQLiteTests(_AvaliacoesCase):
    def migrar(self, comando, revisao, **opcoes):
        from flask_migrate import downgrade, upgrade
        funcao = {"downgrade": downgrade, "upgrade": upgrade}[comando]
        self.db.session.commit()
        self.db.session.remove()
        saida = io.StringIO()
        # Sem o fileConfig() do Alembic, que trocaria os handlers e desligaria os loggers.
        with patch("logging.config.fileConfig"), contextlib.redirect_stdout(saida), \
                contextlib.redirect_stderr(io.StringIO()):
            funcao(directory=str(ROOT / "migrations"), revision=revisao, **opcoes)
        self.servico._esquema_por_engine.clear()
        return saida.getvalue()

    def recusada(self, comando, revisao):
        with self.assertLogs("flask_migrate", "ERROR") as registro, self.assertRaises(SystemExit):
            self.migrar(comando, revisao)
        self.servico._esquema_por_engine.clear()
        return "\n".join(registro.output)

    def revisao(self):
        return self.db.session.scalar(text("SELECT version_num FROM alembic_version"))

    def tabelas(self):
        self.db.session.remove()
        return set(TABELAS) & set(inspect(self.db.engine).get_table_names())

    def executar(self, *comandos):
        self.db.session.remove()
        with self.db.engine.begin() as conexao:
            for comando in comandos:
                conexao.exec_driver_sql(comando)

    def diferencas(self):
        from alembic.autogenerate import compare_metadata
        from alembic.migration import MigrationContext
        self.db.session.remove()
        with self.db.engine.connect() as conexao:
            contexto = MigrationContext.configure(conexao, opts={"compare_type": True})
            return compare_metadata(contexto, self.db.metadata)

    def limpar_tabelas_do_ensaio(self):
        """Só para o próximo cenário do teste: no banco de verdade a revisão nunca apaga tabela."""
        self.executar(*(f"DROP TABLE IF EXISTS {tabela}" for tabela in reversed(TABELAS)))

    def test_uma_head_e_a_revisao_depois_da_frente_m(self):
        from alembic.script import ScriptDirectory
        scripts = ScriptDirectory(str(ROOT / "migrations"))
        self.assertEqual(len(scripts.get_heads()), 1)
        self.assertEqual(scripts.get_revision(REVISAO).down_revision, REVISAO_ANTERIOR)
        self.assertEqual(scripts.get_revision(REVISAO_ANTERIOR).down_revision, "f2b6d8a4c1e9")
        self.assertIn(REVISAO, {r.revision for r in scripts.iterate_revisions(scripts.get_current_head(), "base")})
        self.assertEqual(self.tabelas(), set(TABELAS))
        self.assertEqual(self.diferencas(), [])

    def test_downgrade_recusa_com_dados_e_aceita_vazio_e_o_upgrade_recria(self):
        head = self.revisao()
        self.criar_direto(sugestao="Uma sugestão fictícia.")
        mensagem = self.recusada("downgrade", REVISAO_ANTERIOR)
        self.assertIn("Downgrade das avaliações recusado: há dados de clientes em avaliacao_cliente, "
                      "sugestao_melhoria, evento_avaliacao", mensagem)
        self.assertEqual((self.revisao(), self.tabelas(), len(self.avaliacoes())), (head, set(TABELAS), 1))
        from models import AvaliacaoCliente
        self.db.session.execute(AvaliacaoCliente.__table__.delete())
        self.db.session.commit()
        self.migrar("downgrade", REVISAO_ANTERIOR)
        self.assertEqual((self.revisao(), self.tabelas()), (REVISAO_ANTERIOR, set()))
        # Sem as tabelas, a landing segue e o módulo avisa que está indisponível.
        self.assertEqual(self.contexto(self.usuario(self.cliente_id))["avaliacao_estado"], "indisponivel")
        self.entrar()
        self.assertEqual(self.client.get("/cliente/avaliacao").status_code, 503)
        self.migrar("upgrade", "head")
        self.assertEqual((self.revisao(), self.tabelas(), self.diferencas()), (head, set(TABELAS), []))
        self.assertEqual(self.contexto(self.usuario(self.cliente_id))["avaliacao_estado"], "pode_avaliar")

    def test_adota_as_tabelas_da_c5e8_com_as_linhas_e_cria_o_indice_ausente(self):
        head = self.revisao()
        self.migrar("downgrade", REVISAO_ANTERIOR)
        self.executar(*sql_c5e8("sqlite", sem=("CREATE INDEX ix_evento_avaliacao",)))
        self.executar(
            "INSERT INTO avaliacao_cliente (id_usuario, id_empresa, nota, comentario_publico, status_moderacao, "
            f"data_criacao, data_atualizacao) VALUES ({self.cliente_id}, {self.empresa_id}, 4, "
            "'Comentário gravado antes da revisão.', 'PENDENTE', '2026-11-03 12:00:00', '2026-11-03 12:00:00')",
            f"INSERT INTO evento_avaliacao (id_avaliacao, tipo, ocorrido_em, id_usuario_responsavel) "
            f"VALUES (1, 'CRIADA', '2026-11-03 12:00:00', {self.cliente_id})",
        )
        self.assertNotIn("ix_evento_avaliacao", {i["name"] for i in inspect(self.db.engine).get_indexes("evento_avaliacao")})
        self.migrar("upgrade", "head")
        self.assertEqual((self.revisao(), self.tabelas(), self.diferencas()), (head, set(TABELAS), []))
        self.assertIn("ix_evento_avaliacao", {i["name"] for i in inspect(self.db.engine).get_indexes("evento_avaliacao")})
        [avaliacao] = self.avaliacoes()
        self.assertEqual((avaliacao.nota, avaliacao.comentario_publico, avaliacao.versao, avaliacao.autoriza_publicacao),
                         (4, "Comentário gravado antes da revisão.", 1, False))
        self.assertEqual(self.eventos(avaliacao.id_avaliacao), [("CRIADA", None)])
        self.assertEqual(self.contexto(self.usuario(self.cliente_id))["avaliacao_estado"], "ja_avaliou")

    def test_divergencias_interrompem_sem_mudar_nada(self):
        self.migrar("downgrade", REVISAO_ANTERIOR)
        for troca, esperado in DIVERGENCIAS:
            with self.subTest(esperado=esperado):
                self.executar(*sql_c5e8("sqlite", troca))
                mensagem = self.recusada("upgrade", "head")
                self.assertIn("Revisão d3f5b7a9c1e2 interrompida: as tabelas das avaliações já existem, mas não "
                              "batem com models.py", mensagem)
                self.assertIn(esperado, mensagem)
                self.assertIn("Nada foi alterado", mensagem)
                self.assertEqual((self.revisao(), self.tabelas()), (REVISAO_ANTERIOR, set(TABELAS)))
                self.limpar_tabelas_do_ensaio()
        self.migrar("upgrade", "head")
        self.assertEqual(self.diferencas(), [])

    def test_so_parte_das_tabelas_interrompe(self):
        self.migrar("downgrade", REVISAO_ANTERIOR)
        self.executar(*sql_c5e8("sqlite", sem=("sugestao_melhoria", "evento_avaliacao", "solicitacao_avaliacao")))
        mensagem = self.recusada("upgrade", "head")
        self.assertIn("só parte das tabelas das avaliações existe (presentes: avaliacao_cliente; ausentes: "
                      "sugestao_melhoria, evento_avaliacao, solicitacao_avaliacao)", mensagem)
        self.assertEqual((self.revisao(), self.tabelas()), (REVISAO_ANTERIOR, {"avaliacao_cliente"}))
        # Com o esquema parcial a landing não cai.
        self.assertEqual(self.contexto(self.usuario(self.cliente_id))["avaliacao_estado"], "indisponivel")

    def test_previa_offline_sai_pelo_caminho_da_criacao(self):
        self.migrar("downgrade", REVISAO_ANTERIOR)
        sql = self.migrar("upgrade", f"{REVISAO_ANTERIOR}:{REVISAO}", sql=True)
        for trecho in ("CREATE TABLE avaliacao_cliente", "CREATE TABLE solicitacao_avaliacao",
                       "CREATE INDEX ix_evento_avaliacao", "UPDATE alembic_version SET version_num='d3f5b7a9c1e2'"):
            self.assertIn(trecho, sql)
        self.assertEqual(self.revisao(), REVISAO_ANTERIOR)


# =============================================================================
# 7. Exclusão permanente de empresa com avaliação
# =============================================================================
class ExclusaoPermanenteTests(_AvaliacoesCase):
    def test_exclusao_leva_avaliacao_sugestao_eventos_pedido_e_aviso_aos_admins(self):
        from models import Empresa, Notificacao, SolicitacaoAvaliacao, Usuario
        # Cliente da empresa 1: avaliação pelo modal (aviso aos admins), sugestão, aprovação e o pedido dos 60 dias.
        token = self.entrar()
        self.assertEqual(self.enviar(token, sugestao="Sugestão fictícia.").status_code, 200)
        self.logout()
        [avaliacao] = self.avaliacoes()
        self.aprovar_direto(avaliacao.id_avaliacao)
        with self.app.test_request_context("/"):
            self.servico.registrar_exibicao_solicitacao(self.db.session, self.usuario(self.cliente_id))
        self.db.session.commit()
        # Cliente da empresa 2, que fica.
        outro = self.novo_cliente()
        id_outra = self.criar_direto(outro, autoriza=False)
        self.db.session.get(Empresa, self.empresa_id).deletado_em = datetime(2026, 11, 10)
        self.db.session.commit()
        self.assertEqual(self.db.session.scalar(select(func.count()).select_from(Notificacao).where(
            Notificacao.link_destino == f"/admin/avaliacoes/{avaliacao.id_avaliacao}")), 1)

        token = self.entrar("admin-test@example.invalid")
        resposta = self.client.post(f"/admin/empresa/excluir-permanente/{self.empresa_id}", data={
            "csrf_token": token, "senha_confirmacao": self.password})
        self.assertEqual(resposta.headers["Location"], "/admin/lixeira")
        self.db.session.expire_all()
        self.assertIsNone(self.db.session.get(Empresa, self.empresa_id))
        self.assertIsNone(self.db.session.get(Usuario, self.cliente_id))
        self.assertIsNotNone(self.db.session.get(Usuario, self.admin_id))
        self.assertEqual([a.id_avaliacao for a in self.avaliacoes()], [id_outra])
        for tabela, esperado in (("sugestao_melhoria", 0), ("evento_avaliacao", 1), ("solicitacao_avaliacao", 0)):
            with self.subTest(tabela=tabela):
                self.assertEqual(self.db.session.scalar(text(f"SELECT count(*) FROM {tabela}")), esperado)
        self.assertIsNone(self.db.session.get(SolicitacaoAvaliacao, self.cliente_id))
        self.assertEqual(self.db.session.scalar(select(func.count()).select_from(Notificacao).where(
            Notificacao.texto.like("%Teste 1%"))), 0)

    def test_sem_as_tabelas_a_exclusao_segue(self):
        from models import Empresa
        self.db.session.get(Empresa, self.empresa_id).deletado_em = datetime(2026, 11, 10)
        self.db.session.commit()
        token = self.entrar("admin-test@example.invalid")
        with patch("avaliacoes.esquema_avaliacoes_disponivel", return_value=False):
            resposta = self.client.post(f"/admin/empresa/excluir-permanente/{self.empresa_id}", data={
                "csrf_token": token, "senha_confirmacao": self.password})
        self.assertEqual(resposta.headers["Location"], "/admin/lixeira")
        self.db.session.expire_all()
        self.assertIsNone(self.db.session.get(Empresa, self.empresa_id))


# =============================================================================
# 8. PostgreSQL real (opcional)
# =============================================================================
CHECKS_POSTGRESQL = (
    "private_engine",
    # Adoção das tabelas criadas pela c5e8a1f4b7d2, como no banco do .env
    "schema_before_migration", "legacy_tables_exposed_to_api", "adoption_reaches_head", "adoption_keeps_rows",
    "metadata_matches_models", "rls_enabled_without_force", "policies_for_api_and_server_roles",
    "api_roles_lose_default_privileges", "restrictive_policy_denies_even_with_grant",
    # Aplicação no PostgreSQL
    "modal_post_writes_pending", "unique_constraint_under_concurrency",
    # Downgrade e criação do zero
    "downgrade_refused_with_data", "downgrade_removes_tables_and_policies", "creation_from_scratch",
    # Divergências
    "divergent_type_refused_untouched", "divergent_policy_refused", "permissive_policy_allowing_writes_refused",
    "force_rls_refused", "partial_schema_refused", "upgrade_after_divergences",
    # A deny_api_access permissiva de negação pura que o banco do .env tinha em 17/11/2026
    "production_permissive_deny_normalized",
    # Exclusão definitiva
    "permanent_delete_takes_reviews",
)
POLITICAS_POSTGRESQL = {
    ("deny_api_access", "RESTRICTIVE", "anon,authenticated", "false", "false"),
    ("acesso_servidor", "PERMISSIVE", "postgres,service_role", "true", "true"),
}


class PostgreSQLAvaliacoesTests(unittest.TestCase):
    def test_avaliacoes_em_postgresql_real(self):
        from tests.test_contratos_postgresql import TOOLS, _safe_environment, _tool
        raw_bin = os.environ.get("NEXO_TEST_POSTGRES_BIN")
        if not raw_bin:
            self.skipTest("Defina NEXO_TEST_POSTGRES_BIN para criar um cluster PostgreSQL temporário isolado.")
        self.assertTrue("://" not in raw_bin and Path(raw_bin).is_absolute(),
                        "NEXO_TEST_POSTGRES_BIN aceita apenas um caminho local absoluto; URLs são recusadas.")
        try:
            bin_dir = Path(raw_bin).resolve(strict=True)
        except (OSError, ValueError):
            self.fail("NEXO_TEST_POSTGRES_BIN não indica um diretório local acessível; valor omitido.")
        self.assertTrue(all(_tool(bin_dir, tool).is_file() for tool in TOOLS),
                        "O diretório precisa conter initdb, pg_ctl e createdb.")
        with tempfile.TemporaryDirectory(prefix="nexo-avaliacoes-pg-") as work:
            work_dir = Path(work).resolve()
            data_dir = work_dir / "private-cluster"
            try:
                result = subprocess.run(
                    [sys.executable, "-B", "-m", "tests.test_avaliacoes_clientes", "--isolated-worker",
                     str(bin_dir), str(work_dir)],
                    cwd=str(ROOT), env=_safe_environment(), capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=300, check=False,
                )
            except subprocess.TimeoutExpired:
                self.fail("O ensaio isolado excedeu 300 segundos; nenhuma saída ou credencial foi exibida.")
            finally:
                self.assertTrue(data_dir.resolve().is_relative_to(work_dir))
                if (data_dir / "postmaster.pid").is_file():
                    subprocess.run(
                        [str(_tool(bin_dir, "pg_ctl")), "--pgdata", str(data_dir), "--mode", "fast",
                         "--wait", "--timeout", "20", "stop"],
                        env=_safe_environment(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, timeout=30, check=False,
                    )
        try:
            report = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            self.fail("O processo isolado não produziu um relatório seguro válido; saídas brutas omitidas.")
        self.assertEqual(result.returncode, 0,
                         f"Ensaio isolado falhou na etapa {report.get('stage', 'desconhecida')} "
                         f"({report.get('error', 'erro não identificado')}); saídas brutas omitidas.")
        self.assertEqual(report.get("status"), "ok")
        self.assertEqual(sorted(report.get("checks", [])), sorted(CHECKS_POSTGRESQL))
        self.assertFalse(report.get("production_access"))
        print("\nEnsaio PostgreSQL das avaliações: " + json.dumps(report, ensure_ascii=True, sort_keys=True))


def _cluster_das_avaliacoes():
    """A classe do processo isolado; importada só nele (o módulo dos contratos traz o cluster privado)."""
    from tests.test_contratos_postgresql import MIGRATIONS, PASSWORD, _PrivateCluster

    class _ClusterDasAvaliacoes(_PrivateCluster):
        def _etapas(self):
            self._etapa_preparacao()
            self._etapa_adocao()
            self._etapa_aplicacao()
            self._etapa_downgrade_e_criacao()
            self._etapa_divergencias()
            self._etapa_exclusao_definitiva()

        @staticmethod
        def _limpar_cache_esquema():
            import avaliacoes
            _PrivateCluster._limpar_cache_esquema()
            avaliacoes._esquema_por_engine.clear()

        # ---------------------------------------------------------- apoio
        def _sql(self, consulta, **parametros):
            with self.app.app_context():
                with self.db.engine.connect() as conexao:
                    return conexao.execute(text(consulta), parametros).all()

        def _executar(self, *comandos, **parametros):
            with self.app.app_context():
                with self.db.engine.begin() as conexao:
                    for comando in comandos:
                        conexao.execute(text(comando), parametros)

        def _ddl(self, comandos):
            with self.app.app_context():
                with self.db.engine.begin() as conexao:
                    for comando in comandos:
                        conexao.exec_driver_sql(comando)

        def _apagar_tabelas_do_ensaio(self):
            self._ddl([f"DROP TABLE IF EXISTS {tabela}" for tabela in reversed(TABELAS)])

        def _migrar_registrando(self, operacao, revisao):
            """(True, '') quando passa; (False, mensagem do Flask-Migrate) quando a revisão recusa."""
            mensagens = []
            manipulador = logging.Handler()
            manipulador.emit = lambda registro: mensagens.append(registro.getMessage())
            registro = logging.getLogger("flask_migrate")
            desligado = registro.disabled
            registro.disabled = False
            registro.addHandler(manipulador)
            try:
                with self.app.app_context():
                    self.db.session.remove()
                self._migrar(operacao, revisao)
                return True, ""
            except RuntimeError:
                return False, " ".join(mensagens)
            finally:
                registro.removeHandler(manipulador)
                registro.disabled = desligado
                self._limpar_cache_esquema()

        def _politicas(self):
            politicas = {}
            for tabela, nome, tipo, papeis, usando, checando in self._sql(
                    "SELECT tablename, policyname, permissive, "
                    "array_to_string(ARRAY(SELECT unnest(roles) ORDER BY 1), ','), qual, with_check "
                    "FROM pg_policies WHERE schemaname = current_schema() AND tablename = ANY(:tabelas)",
                    tabelas=list(TABELAS)):
                politicas.setdefault(tabela, set()).add((nome, tipo, papeis, usando, checando))
            return politicas

        def _rls(self):
            return {nome: (bool(ligado), bool(forcado)) for nome, ligado, forcado in self._sql(
                "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relname = ANY(:tabelas) AND relkind = 'r'", tabelas=list(TABELAS))}

        def _privilegios_da_api(self):
            return [tuple(linha) for linha in self._sql(
                "SELECT papel, tabela FROM unnest(ARRAY['anon', 'authenticated']) papel, unnest(:tabelas) tabela "
                "WHERE has_table_privilege(papel, tabela, 'SELECT') OR has_table_privilege(papel, tabela, 'INSERT') "
                "OR has_table_privilege(papel, tabela, 'TRUNCATE')", tabelas=list(TABELAS))]

        def _quantas(self, tabela="avaliacao_cliente"):
            return self._sql(f"SELECT count(*) FROM {tabela}")[0][0]

        def _protegidas(self):
            return (self._rls() == {t: (True, False) for t in TABELAS}
                    and self._politicas() == {t: POLITICAS_POSTGRESQL for t in TABELAS}
                    and self._privilegios_da_api() == [])

        # ---------------------------------------------------------- 1. adoção
        def _etapa_preparacao(self):
            from flask_migrate import upgrade
            from models import Plano, Segmento, Usuario
            self.stage = "migração até a revisão anterior"
            self._migrar(upgrade, REVISAO_ANTERIOR)
            self._limpar_cache_esquema()
            self.check("schema_before_migration",
                       self._revisao() == REVISAO_ANTERIOR and not self._tabelas() & set(TABELAS))
            self.stage = "papéis da Data API do Supabase"
            self._executar(
                'CREATE ROLE "anon" NOLOGIN', 'CREATE ROLE "authenticated" NOLOGIN',
                'CREATE ROLE "service_role" NOLOGIN BYPASSRLS', 'CREATE ROLE "postgres" NOLOGIN',
                # O que o Supabase faz no schema public: tabela nova nasce acessível pela Data API.
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO anon, authenticated",
                "GRANT USAGE ON SCHEMA public TO anon, authenticated",
            )
            self.stage = "dados fictícios"
            with self._sessao() as sessao:
                plano = Plano(nome_plano="BRONZE", valor_mensal=Decimal("100"), qtd_analises_mes=1,
                              tipo_analise_permitida="MENSAL", nivel_entrega_analise="BASICA",
                              nivel_dashboard="RESUMIDO", nivel_atendimento="BAIXO")
                segmento = Segmento(nome_segmento="Avaliações sintéticas")
                admin = Usuario(nome="Admin fictício", email="admin-avaliacoes@example.invalid", role="ADMIN",
                                primeiro_acesso=False)
                admin.set_senha(PASSWORD)
                sessao.add_all([plano, segmento, admin])
                sessao.commit()
                self.plano_id, self.segmento_id, self.admin_id = plano.id_plano, segmento.id_segmento, admin.id_usuario
            for nome in ("E1", "E2", "E3"):
                self._nova_empresa(nome, senha=True)

        def _etapa_adocao(self):
            from flask_migrate import upgrade
            import avaliacoes
            self.stage = "tabelas da c5e8a1f4b7d2 com uma avaliação"
            self._ddl(sql_c5e8("postgresql"))
            id_empresa, id_cliente = self.ids["E1"]
            self._executar(
                "INSERT INTO avaliacao_cliente (id_usuario, id_empresa, nota, comentario_publico, status_moderacao, "
                "data_criacao, data_atualizacao) VALUES (:u, :e, 4, 'Comentário de antes da revisão.', 'PENDENTE', "
                "now(), now())", u=id_cliente, e=id_empresa)
            self._executar("INSERT INTO evento_avaliacao (id_avaliacao, tipo, ocorrido_em, id_usuario_responsavel) "
                           "SELECT id_avaliacao, 'CRIADA', now(), id_usuario FROM avaliacao_cliente")
            expostas = self._privilegios_da_api()
            self.check("legacy_tables_exposed_to_api", len(expostas) == 8 and self._rls() == {
                t: (False, False) for t in TABELAS}, str(expostas))

            self.stage = "adoção pela d3f5b7a9c1e2"
            passou, mensagem = self._migrar_registrando(upgrade, "head")
            from alembic.script import ScriptDirectory
            self.head = ScriptDirectory(MIGRATIONS).get_current_head()
            self.check("adoption_reaches_head", passou and self._revisao() == self.head, mensagem[:300])
            with self.app.app_context():
                adotada = avaliacoes.obter_avaliacao_usuario(id_cliente)
                linha = (adotada.nota, adotada.comentario_publico, adotada.versao, adotada.autoriza_publicacao)
                self.db.session.remove()
            self.check("adoption_keeps_rows", linha == (4, "Comentário de antes da revisão.", 1, False)
                       and self._quantas("evento_avaliacao") == 1, str(linha))
            diferencas = self._diferencas_metadata()
            self.check("metadata_matches_models", diferencas == [], repr(diferencas)[:400])
            self.check("rls_enabled_without_force", self._rls() == {t: (True, False) for t in TABELAS}, str(self._rls()))
            self.check("policies_for_api_and_server_roles",
                       self._politicas() == {t: POLITICAS_POSTGRESQL for t in TABELAS}, str(self._politicas())[:400])
            self.check("api_roles_lose_default_privileges", self._privilegios_da_api() == [],
                       str(self._privilegios_da_api()))

            self.stage = "política restritiva com GRANT posterior"
            from sqlalchemy.exc import DBAPIError
            self._executar("GRANT SELECT, INSERT ON avaliacao_cliente TO anon")
            with self.app.app_context():
                with self.db.engine.connect() as conexao:
                    transacao = conexao.begin()
                    conexao.execute(text("SET LOCAL ROLE anon"))
                    visiveis = conexao.scalar(text("SELECT count(*) FROM avaliacao_cliente"))
                    try:
                        conexao.execute(text(
                            "INSERT INTO avaliacao_cliente (id_usuario, id_empresa, nota, comentario_publico, "
                            "status_moderacao, data_criacao, data_atualizacao) "
                            "VALUES (1, 1, 5, 'Invasor', 'APROVADA', now(), now())"))
                        inserido = True
                    except DBAPIError:
                        inserido = False
                    transacao.rollback()
            self._executar("REVOKE ALL ON avaliacao_cliente FROM anon")
            self.check("restrictive_policy_denies_even_with_grant", visiveis == 0 and inserido is False,
                       f"visiveis={visiveis} inserido={inserido}")

        # ---------------------------------------------------------- 2. aplicação
        def _etapa_aplicacao(self):
            import avaliacoes
            from sqlalchemy.exc import IntegrityError
            from sqlalchemy.orm import Session
            from models import Usuario
            self.stage = "envio pelo modal no PostgreSQL"
            cliente, entrada = self._login("cliente-e2@example.invalid")
            token = self._csrf(cliente, "/cliente/dashboard")
            resposta = cliente.post("/cliente/avaliacao", headers=FETCH, data={
                "csrf_token": token, "nota": "5", "comentario": COMENTARIO, "autoriza_publicacao": "1"})
            corpo = resposta.get_json() or {}
            linha = self._sql("SELECT status_moderacao, autoriza_publicacao FROM avaliacao_cliente WHERE id_usuario = :u",
                              u=self.ids["E2"][1])
            self.check("modal_post_writes_pending", entrada.status_code == 302 and resposta.status_code == 200
                       and corpo.get("ok") is True and [tuple(l) for l in linha] == [("PENDENTE", True)],
                       f"status={resposta.status_code} linha={linha}")

            self.stage = "dois envios simultâneos do mesmo cliente"
            id_cliente = self.ids["E3"][1]
            gravou, primeiro_pode_seguir, erros = threading.Event(), threading.Event(), []

            def enviar(primeiro):
                try:
                    with self.app.app_context():
                        with Session(bind=self.db.engine) as sessao:
                            usuario = sessao.get(Usuario, id_cliente)
                            dados = avaliacoes.DadosAvaliacao(4, COMENTARIO, None, False, None, False)
                            avaliacoes.criar_avaliacao(sessao, usuario, dados)
                            if primeiro:
                                gravou.set()
                                primeiro_pode_seguir.wait(10)
                            sessao.commit()
                except IntegrityError as erro:
                    erros.append(str(erro.orig))
                except BaseException as erro:  # noqa: BLE001 - levado à verificação
                    erros.append(type(erro).__name__)

            primeira = threading.Thread(target=enviar, args=(True,), daemon=True)
            primeira.start()
            gravou.wait(10)
            segunda = threading.Thread(target=enviar, args=(False,), daemon=True)
            segunda.start()
            segunda.join(1.0)  # a segunda espera o índice único da primeira
            esperando = segunda.is_alive()
            primeiro_pode_seguir.set()
            primeira.join(15)
            segunda.join(15)
            quantas = self._sql("SELECT count(*) FROM avaliacao_cliente WHERE id_usuario = :u", u=id_cliente)[0][0]
            self.check("unique_constraint_under_concurrency",
                       esperando and quantas == 1 and len(erros) == 1 and "uq_avaliacao_cliente_usuario" in erros[0],
                       f"esperando={esperando} quantas={quantas} erros={[e[:80] for e in erros]}")

        # ---------------------------------------------------------- 3. downgrade e criação do zero
        def _etapa_downgrade_e_criacao(self):
            from flask_migrate import downgrade, upgrade
            self.stage = "downgrade com avaliações gravadas"
            antes = self._quantas()
            passou, mensagem = self._migrar_registrando(downgrade, REVISAO_ANTERIOR)
            self.check("downgrade_refused_with_data",
                       not passou and "Downgrade das avaliações recusado" in mensagem
                       and self._revisao() == self.head and self._quantas() == antes, mensagem[:200])
            self.stage = "downgrade sem avaliações"
            self._executar("DELETE FROM avaliacao_cliente", "DELETE FROM solicitacao_avaliacao")
            passou, mensagem = self._migrar_registrando(downgrade, REVISAO_ANTERIOR)
            self.check("downgrade_removes_tables_and_policies",
                       passou and self._revisao() == REVISAO_ANTERIOR and not self._tabelas() & set(TABELAS)
                       and self._politicas() == {}, mensagem[:200])
            self.stage = "criação do zero"
            passou, mensagem = self._migrar_registrando(upgrade, "head")
            self.check("creation_from_scratch", passou and self._revisao() == self.head and self._protegidas()
                       and self._diferencas_metadata() == [], mensagem[:200])

        # ---------------------------------------------------------- 4. divergências
        def _etapa_divergencias(self):
            from flask_migrate import downgrade, upgrade
            self.stage = "divergências na adoção"
            self._migrar_registrando(downgrade, REVISAO_ANTERIOR)

            self._ddl(sql_c5e8("postgresql", ("nota INTEGER NOT NULL", "nota SMALLINT NOT NULL")))
            passou, mensagem = self._migrar_registrando(upgrade, "head")
            self.check("divergent_type_refused_untouched",
                       not passou and "avaliacao_cliente.nota: tipo SMALLINT (esperado INTEGER)" in mensagem
                       and self._revisao() == REVISAO_ANTERIOR and self._rls() == {t: (False, False) for t in TABELAS}
                       and len(self._privilegios_da_api()) == 8, mensagem[:300])
            self._apagar_tabelas_do_ensaio()

            self._ddl(sql_c5e8("postgresql") + [
                "ALTER TABLE avaliacao_cliente ENABLE ROW LEVEL SECURITY",
                "CREATE POLICY deny_api_access ON avaliacao_cliente AS PERMISSIVE FOR ALL TO anon USING (true)"])
            passou, mensagem = self._migrar_registrando(upgrade, "head")
            self.check("divergent_policy_refused",
                       not passou and "avaliacao_cliente: a política deny_api_access já existe com outra definição"
                       in mensagem and self._revisao() == REVISAO_ANTERIOR, mensagem[:300])
            self._apagar_tabelas_do_ensaio()

            # Permissiva que nega a leitura mas deixa a API gravar: não é a negação pura, continua divergência.
            self._ddl(sql_c5e8("postgresql") + [
                "ALTER TABLE evento_avaliacao ENABLE ROW LEVEL SECURITY",
                "CREATE POLICY deny_api_access ON evento_avaliacao AS PERMISSIVE FOR ALL TO anon, authenticated "
                "USING (false) WITH CHECK (true)"])
            passou, mensagem = self._migrar_registrando(upgrade, "head")
            self.check("permissive_policy_allowing_writes_refused",
                       not passou and "evento_avaliacao: a política deny_api_access já existe com outra definição"
                       in mensagem and self._revisao() == REVISAO_ANTERIOR
                       and self._politicas().get("evento_avaliacao") == {
                           ("deny_api_access", "PERMISSIVE", "anon,authenticated", "false", "true")}, mensagem[:300])
            self._apagar_tabelas_do_ensaio()

            self._ddl(sql_c5e8("postgresql") + ["ALTER TABLE sugestao_melhoria ENABLE ROW LEVEL SECURITY",
                                                "ALTER TABLE sugestao_melhoria FORCE ROW LEVEL SECURITY"])
            passou, mensagem = self._migrar_registrando(upgrade, "head")
            self.check("force_rls_refused", not passou and "sugestao_melhoria: FORCE ROW LEVEL SECURITY ligado" in mensagem
                       and self._revisao() == REVISAO_ANTERIOR, mensagem[:300])
            self._apagar_tabelas_do_ensaio()

            self._ddl(sql_c5e8("postgresql", sem=("CREATE TABLE evento_avaliacao", "ix_evento_avaliacao")))
            passou, mensagem = self._migrar_registrando(upgrade, "head")
            self.check("partial_schema_refused", not passou and "só parte das tabelas" in mensagem
                       and "ausentes: evento_avaliacao" in mensagem and self._revisao() == REVISAO_ANTERIOR,
                       mensagem[:300])
            self._apagar_tabelas_do_ensaio()

            passou, mensagem = self._migrar_registrando(upgrade, "head")
            self.check("upgrade_after_divergences", passou and self._revisao() == self.head and self._protegidas(),
                       mensagem[:200])

            # O banco do .env em 17/11/2026: RLS ligado nas quatro, deny_api_access PERMISSIVE FOR ALL TO anon,
            # authenticated USING (false) sem WITH CHECK, sem acesso_servidor e com todos os privilégios da Data API.
            self.stage = "deny_api_access permissiva de negação pura (banco do .env em 17/11)"
            self._migrar_registrando(downgrade, REVISAO_ANTERIOR)
            self._ddl(sql_c5e8("postgresql")
                      + [f"ALTER TABLE {tabela} ENABLE ROW LEVEL SECURITY" for tabela in TABELAS]
                      + [f"CREATE POLICY deny_api_access ON {tabela} AS PERMISSIVE FOR ALL TO anon, authenticated "
                         "USING (false)" for tabela in TABELAS])
            antes = (self._politicas(), len(self._privilegios_da_api()))
            passou, mensagem = self._migrar_registrando(upgrade, "head")
            self.check("production_permissive_deny_normalized",
                       antes == ({t: {("deny_api_access", "PERMISSIVE", "anon,authenticated", "false", None)}
                                  for t in TABELAS}, 8)
                       and passou and self._revisao() == self.head and self._protegidas()
                       and self._diferencas_metadata() == [],
                       f"{mensagem[:200]} antes={str(antes)[:200]} depois={str(self._politicas())[:200]}")

        # ---------------------------------------------------------- 5. exclusão definitiva
        def _etapa_exclusao_definitiva(self):
            import avaliacoes
            from models import Usuario
            self.stage = "exclusão definitiva de empresa com avaliação"
            id_empresa, id_cliente = self.ids["E1"]
            with self._sessao() as sessao:
                usuario = sessao.get(Usuario, id_cliente)
                dados = avaliacoes.DadosAvaliacao(5, COMENTARIO, "Sugestão fictícia.", True, None, False)
                avaliacao = avaliacoes.criar_avaliacao(sessao, usuario, dados)
                avaliacoes.aprovar_avaliacao(sessao, avaliacao.id_avaliacao, sessao.get(Usuario, self.admin_id), 1)
                # A da outra empresa fica.
                avaliacoes.criar_avaliacao(sessao, sessao.get(Usuario, self.ids["E2"][1]),
                                           avaliacoes.DadosAvaliacao(4, COMENTARIO, None, False, None, False))
                sessao.commit()
            outras = self._sql("SELECT count(*) FROM avaliacao_cliente WHERE id_empresa <> :e", e=id_empresa)[0][0]
            self._executar("UPDATE empresa SET deletado_em = now() WHERE id_empresa = :e", e=id_empresa)
            admin, _ = self._login("admin-avaliacoes@example.invalid")
            resposta = admin.post(f"/admin/empresa/excluir-permanente/{id_empresa}", data={
                "csrf_token": self._csrf(admin, "/admin/lixeira"), "senha_confirmacao": PASSWORD})
            sobras = {tabela: self._sql(f"SELECT count(*) FROM {tabela}")[0][0] for tabela in TABELAS}
            restantes = self._sql("SELECT count(*) FROM avaliacao_cliente WHERE id_empresa <> :e", e=id_empresa)[0][0]
            empresa = self._sql("SELECT count(*) FROM empresa WHERE id_empresa = :e", e=id_empresa)[0][0]
            self.check("permanent_delete_takes_reviews",
                       resposta.status_code == 302 and resposta.headers.get("Location") == "/admin/lixeira"
                       and empresa == 0 and restantes == outras == 1 and sobras["avaliacao_cliente"] == 1
                       and sobras["sugestao_melhoria"] == 0,
                       f"status={resposta.status_code} sobras={sobras} restantes={restantes}/{outras}")

    return _ClusterDasAvaliacoes


def _worker_main(bin_dir, work_dir):
    cluster = _cluster_das_avaliacoes()(Path(bin_dir).resolve(), Path(work_dir).resolve())
    # Não encaminha mensagens de Flask, SQLAlchemy, Alembic ou PostgreSQL.
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            report = cluster.run()
    except Exception as error:
        detail = f": {error}" if isinstance(error, AssertionError) else ""
        print(json.dumps({"status": "error", "stage": cluster.stage, "error": type(error).__name__ + detail}))
        return 1
    print(json.dumps(report))
    return 0


# =============================================================================
# 9. Navegador real (opcional)
# =============================================================================
_BROWSER = os.environ.get("NEXO_TEST_BROWSER") == "1" and sync_playwright is not None
_CDN_BOOTSTRAP = "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css"
# A página do ensaio enquanto a landing não inclui o parcial: as mesmas folhas e o mesmo script que a landing
# carrega para a seção (contratos.md, seção 3). Depois da junção, o ensaio usa a própria landing.
PAGINA_DE_ENSAIO = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Ensaio das avaliações</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet"
        integrity="sha384-QWTKZyjpPEjISv5WaRU9OFeRpok6YctnYmDr5pNlyT2bRjXh0JMhjY6hW+ALEwIH" crossorigin="anonymous">
  <link rel="stylesheet" href="{{ url_for('static', filename='css/landing.css') }}">
  <link rel="stylesheet" href="{{ url_for('static', filename='css/landing-avaliacao.css') }}">
  <script defer src="{{ url_for('static', filename='js/landing-avaliacao.js') }}"></script>
</head>
<body>
<main id="conteudo">
  <section id="inicio"><div class="container"><h1>Ensaio</h1><p>Seção anterior.</p></div></section>
  <section id="avaliacoes" class="reviews-section" aria-labelledby="reviews-title">
    <div class="container">
      <h2 id="reviews-title" class="section-title">Avaliações</h2>
      {% include "_avaliacao_landing.html" %}
    </div>
  </section>
</main>
</body>
</html>"""
_COLETOR_CSP = """
window.__cspViolations = [];
document.addEventListener('securitypolicyviolation', function (e) {
  window.__cspViolations.push(e.violatedDirective + ' ' + e.blockedURI);
});
"""
_MEDIR_MODAL = """() => {
  const vw = innerWidth, caixa = document.getElementById('avaliacao-modal').getBoundingClientRect();
  const controles = [...document.querySelectorAll('#avaliacao-modal a, #avaliacao-modal button, #avaliacao-modal textarea,'
    + ' #avaliacao-modal .avaliacao-modal-estrela, #avaliacao-modal input[type=checkbox]')]
    .filter(el => el.getClientRects().length);
  const fora = controles.filter(el => { const b = el.getBoundingClientRect();
    return b.left < caixa.left - 1 || b.right > caixa.right + 1; }).map(el => el.id || el.className || el.tagName);
  const estrelas = [...document.querySelectorAll('.avaliacao-modal-estrela')].map(el => {
    const b = el.getBoundingClientRect(); return [Math.round(b.width), Math.round(b.height)]; });
  const botoes = [...document.querySelectorAll('#avaliacao-modal .avaliacao-modal-acoes .btn-nexo, .avaliacao-modal-fechar')]
    .filter(el => el.getClientRects().length).map(el => Math.round(el.getBoundingClientRect().height));
  return {rola: document.documentElement.scrollWidth > vw + 1, x0: caixa.left, x1: caixa.right, vw, fora, estrelas,
          botoes, caixaRola: document.getElementById('avaliacao-modal').scrollWidth > caixa.width + 1};
}"""


@unittest.skipUnless(_BROWSER, "Defina NEXO_TEST_BROWSER=1 e instale o Playwright para validar o modal no navegador.")
class AvaliacaoNavegadorTests(_AvaliacoesCase):
    def setUp(self):
        super().setUp()
        from werkzeug.serving import make_server
        from flask_login import current_user
        from tests.test_contratos_navegador import _copias_locais_da_cdn
        self.cdn_local = _copias_locais_da_cdn()
        if _CDN_BOOTSTRAP not in self.cdn_local:
            # Sem o bootstrap.min.css íntegro a medida de layout não diz nada sobre o produto (a CI baixa a cópia).
            self.skipTest("Bootstrap 5.3.3 íntegro indisponível (NEXO_TEST_CDN_DIR)")
        fonte = self.app.jinja_env.loader.get_source(self.app.jinja_env, "landing.html")[0]
        self.landing_real = "_avaliacao_landing.html" in fonte
        servico = self.servico

        def pagina_inicial():
            contexto = servico.contexto_da_landing(current_user)
            if self.landing_real:
                return render_template("landing.html", **contexto)
            return render_template_string(PAGINA_DE_ENSAIO, **contexto)

        self.app.view_functions["index"] = pagina_inicial
        for alvo, original in _CONECTAR_LOCAL.items():
            self.stack.enter_context(patch(alvo, original))
        self.servidor = make_server("127.0.0.1", 0, self.app, threaded=True)
        self.base = f"http://127.0.0.1:{self.servidor.server_port}"
        threading.Thread(target=self.servidor.serve_forever, daemon=True).start()
        self.addCleanup(self._parar_servidor)
        self.pw = sync_playwright().start()
        self.addCleanup(self.pw.stop)
        self.navegador = self.pw.chromium.launch(executable_path=os.environ.get("NEXO_TEST_CHROMIUM") or None)
        self.addCleanup(self.navegador.close)
        self.erros_js = []

    def _parar_servidor(self):
        self.servidor.shutdown()
        self.servidor.server_close()

    def tearDown(self):
        self.assertEqual(self.erros_js, [], "Erros de JavaScript na página")

    def _rota(self, rota):
        url = rota.request.url
        if url.startswith(self.base + "/"):
            rota.continue_()
        elif url in self.cdn_local:
            caminho, tipo = self.cdn_local[url]
            rota.fulfill(status=200, path=caminho, content_type=tipo, headers={"Access-Control-Allow-Origin": "*"})
        else:
            rota.abort()  # nada sai da máquina: fontes e gráficos ficam de fora

    def abrir(self, largura, altura=800, *, javascript=True):
        contexto = self.navegador.new_context(viewport={"width": largura, "height": altura}, locale="pt-BR",
                                              java_script_enabled=javascript, reduced_motion="reduce")
        self.addCleanup(contexto.close)
        pagina = contexto.new_page()
        pagina.on("pageerror", lambda erro: self.erros_js.append(f"{largura}px: {erro}"))
        pagina.add_init_script(_COLETOR_CSP)
        pagina.route("**/*", self._rota)
        return pagina

    def entrar_no_navegador(self, pagina, caminho="/auth/login"):
        pagina.goto(self.base + caminho, wait_until="load")
        pagina.fill("#email", "cliente-test@example.invalid")
        pagina.fill("#senha", self.password)
        with pagina.expect_navigation(wait_until="load"):
            pagina.click("button[type=submit]")

    def ativo(self, pagina):
        return pagina.evaluate("document.activeElement && (document.activeElement.id || "
                               "document.activeElement.getAttribute('data-avaliacao-abrir') !== null && 'gatilho' || "
                               "document.activeElement.tagName)")

    def csp(self, pagina):
        return pagina.evaluate("window.__cspViolations || []")

    def test_do_visitante_ao_envio_pelo_teclado(self):
        pagina = self.abrir(390, 844)
        pagina.goto(self.base + "/", wait_until="load")
        link = pagina.locator("[data-avaliacao-estado=visitante] a")
        self.assertEqual((link.inner_text().strip(), link.get_attribute("href")), ("Deixar Avaliação", "/avaliar"))
        with pagina.expect_navigation(wait_until="load"):
            link.click()
        self.assertTrue(pagina.url.endswith("/auth/login?next=/avaliar"), pagina.url)
        pagina.fill("#email", "cliente-test@example.invalid")
        pagina.fill("#senha", self.password)
        with pagina.expect_navigation(wait_until="load"):
            pagina.click("button[type=submit]")
        dialogo = pagina.locator("#avaliacao-modal")
        dialogo.wait_for(state="visible")
        # O parâmetro saiu do endereço (recarregar não reabre) e a âncora ficou.
        self.assertTrue(pagina.url.endswith("/#avaliacoes"), pagina.url)
        self.assertEqual(self.ativo(pagina), "avaliacao-nota-1")
        for _ in range(3):
            pagina.keyboard.press("ArrowRight")
        self.assertTrue(pagina.locator("#avaliacao-nota-4").is_checked())
        self.assertEqual(self.ativo(pagina), "avaliacao-nota-4")
        self.assertEqual(pagina.locator("[data-avaliacao-significado]").inner_text(), "4 estrelas: Satisfeito")
        pagina.keyboard.press("Tab")
        self.assertEqual(self.ativo(pagina), "avaliacao-comentario")
        pagina.keyboard.type("Atendimento muito bom e relatórios claros.")
        self.assertEqual(pagina.locator("[data-avaliacao-contador]").inner_text(), "42/1000")
        pagina.keyboard.press("Tab")
        self.assertEqual(self.ativo(pagina), "avaliacao-autoriza")
        self.assertFalse(pagina.locator("#avaliacao-autoriza").is_checked())
        pagina.keyboard.press("Space")
        self.assertTrue(pagina.locator("#avaliacao-autoriza").is_checked())
        # Foco preso: do último ao primeiro e de volta.
        pagina.get_by_role("button", name="Cancelar").focus()
        pagina.keyboard.press("Tab")
        self.assertTrue(pagina.evaluate("document.activeElement.classList.contains('avaliacao-modal-fechar')"))
        pagina.keyboard.press("Shift+Tab")
        self.assertEqual(pagina.evaluate("document.activeElement.textContent.trim()"), "Cancelar")
        pagina.locator("[data-avaliacao-enviar]").focus()
        pagina.keyboard.press("Enter")
        final = pagina.locator("[data-avaliacao-final-texto]")
        final.wait_for(state="visible")
        self.assertEqual(final.inner_text(), self.servico.MENSAGEM_RECEBIDA)
        self.assertEqual(pagina.evaluate("document.activeElement.hasAttribute('data-avaliacao-final-texto')"), True)
        [avaliacao] = self.avaliacoes()
        self.assertEqual((avaliacao.nota, avaliacao.autoriza_publicacao, avaliacao.status_moderacao), (4, True, "PENDENTE"))
        pagina.keyboard.press("Escape")
        dialogo.wait_for(state="hidden")
        gatilho = pagina.locator("[data-avaliacao-abrir]")
        self.assertEqual((gatilho.inner_text().strip(), gatilho.get_attribute("href"), gatilho.get_attribute("role")),
                         ("Ver minha avaliação", "/cliente/minha-avaliacao", None))
        self.assertEqual(self.ativo(pagina), "gatilho")
        self.assertEqual(self.csp(pagina), [])

    def test_erros_por_campo_esc_e_reabertura_pelo_teclado(self):
        pagina = self.abrir(1366, 768)
        self.entrar_no_navegador(pagina)
        pagina.goto(self.base + "/#avaliacoes", wait_until="load")
        gatilho = pagina.locator("[data-avaliacao-abrir]")
        self.assertEqual((gatilho.get_attribute("role"), gatilho.get_attribute("aria-haspopup")), ("button", "dialog"))
        gatilho.click()
        pagina.locator("#avaliacao-modal").wait_for(state="visible")
        pagina.locator("[data-avaliacao-enviar]").click()
        resumo = pagina.locator("[data-avaliacao-resumo]")
        resumo.wait_for(state="visible")
        self.assertIn("Confira os campos destacados.", resumo.inner_text())
        self.assertEqual(pagina.locator("#avaliacao-nota-grupo").get_attribute("aria-invalid"), "true")
        self.assertEqual(pagina.locator("#avaliacao-comentario").get_attribute("aria-invalid"), "true")
        self.assertEqual(self.ativo(pagina), "avaliacao-nota-1")
        # Corrigir a nota tira o erro dela do resumo sem reescrever o resto.
        pagina.keyboard.press("Space")
        self.assertIsNone(pagina.locator("#avaliacao-nota-grupo").get_attribute("aria-invalid"))
        self.assertEqual(resumo.locator("li").count(), 1)
        pagina.keyboard.press("Escape")
        pagina.locator("#avaliacao-modal").wait_for(state="hidden")
        self.assertEqual(self.ativo(pagina), "gatilho")
        pagina.keyboard.press("Enter")
        pagina.locator("#avaliacao-modal").wait_for(state="visible")
        self.assertEqual(self.ativo(pagina), "avaliacao-nota-1")
        # O "Cancelar" fecha e devolve o foco; nada foi gravado.
        pagina.get_by_role("button", name="Cancelar").click()
        pagina.locator("#avaliacao-modal").wait_for(state="hidden")
        self.assertEqual(self.ativo(pagina), "gatilho")
        self.assertEqual(self.avaliacoes(), [])
        self.assertEqual(self.csp(pagina), [])

    def test_modal_cabe_em_320_375_e_390_px_sem_rolagem_horizontal(self):
        for largura, altura in ((320, 568), (375, 667), (390, 844)):
            with self.subTest(largura=largura):
                pagina = self.abrir(largura, altura)
                self.entrar_no_navegador(pagina)
                pagina.goto(self.base + "/?avaliar=1#avaliacoes", wait_until="load")
                pagina.locator("#avaliacao-modal").wait_for(state="visible")
                medida = pagina.evaluate(_MEDIR_MODAL)
                self.assertFalse(medida["rola"], f"{largura}px: a página rola na horizontal")
                self.assertFalse(medida["caixaRola"], f"{largura}px: o modal rola na horizontal")
                self.assertGreaterEqual(medida["x0"], 0, medida)
                self.assertLessEqual(medida["x1"], medida["vw"], medida)
                self.assertEqual(medida["fora"], [], f"{largura}px: controle fora do modal")
                self.assertEqual(medida["estrelas"], [[48, 48]] * 5, medida)
                self.assertTrue(all(altura_botao >= 44 for altura_botao in medida["botoes"]), medida)
                pagina.context.close()

    def test_sem_javascript_o_botao_leva_ao_formulario_do_portal(self):
        pagina = self.abrir(390, 844, javascript=False)
        self.entrar_no_navegador(pagina)
        pagina.goto(self.base + "/#avaliacoes", wait_until="load")
        self.assertFalse(pagina.locator("#avaliacao-modal").is_visible())
        link = pagina.locator("[data-avaliacao-abrir]")
        self.assertEqual(link.get_attribute("href"), "/cliente/avaliacao")
        with pagina.expect_navigation(wait_until="load"):
            link.click()
        self.assertTrue(pagina.url.endswith("/cliente/avaliacao"), pagina.url)
        self.assertIn("Avalie sua experiência com a NEXO", pagina.locator(".page-head h1").inner_text())


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--isolated-worker":
        raise SystemExit(_worker_main(sys.argv[2], sys.argv[3]))
    unittest.main()
