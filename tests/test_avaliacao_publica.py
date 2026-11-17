"""Raio-X público do lead (blueprints/avaliacao_publica.py; decisões do dono em 15/11/2026).

  1. Desligado por padrão: as quatro rotas respondem 404 e o "REALIZAR RAIO-X" do pré-cadastro some.
  2. Ligado: o envio grava o lead pelo mesmo caminho do consultor (origem "site", sem consultor, com a
     autorização e o que o visitante informou), sem disparar WhatsApp, e redireciona para o resultado.
  3. Vitrine por lista branca: por bloco, só o produto de maior impacto; dos outros não sai nada (nem nome,
     com o diagnóstico completo do consultor como gabarito); R$ do bloco só com 3 produtos ou mais.
  4. O resultado só pela sessão de quem enviou, até 8 horas; "Encerrar" apaga o vínculo.
  5. Pedido inválido (campos, autorização, isca, tempo mínimo, arquivos, tamanho) não lê arquivo, não grava
     e não toca nas tabelas de lead; teto diário, vaga ocupada e limite por IP.
  6. Migração e7a1c3f5b9d2: o downgrade recusa com lead do site e, sem ele, sai e volta; sem a coluna, as
     telas avisam em vez de quebrar.
O processo isolado é o worker em processo (tests/raio_x_support). Dados fictícios.
"""

import contextlib
import io
from decimal import Decimal
import json
from pathlib import Path
import re
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import uuid

from flask import render_template
from sqlalchemy import event, select

import avaliacao_motor
import diagnostico_cenario
from tests.raio_x_support import avaliar_como_o_worker
from tests.test_avaliacao_cenario import LOTE_MESMOS_MESES, _BancoCase, _relatorio
from tests.test_overflow_navegador import _BROWSER, NavegadorCase
from tests.test_whatsapp_navegador import _COLETOR_CSP
from tests.test_whatsapp_telas import NOVA_ABA, _Pagina

NUMERO = "5562988887777"
PAGINA = "/avaliacao-inicial"
RESULTADO = "/avaliacao-inicial/resultado"
SAIR = "/avaliacao-inicial/sair"
EMPRESA = "Mercado Fictício da Maria"


def lote_de_sentinelas(produtos=40):
    """24 relatórios mensais (2024 e 2025) com nomes que não são prefixo uns dos outros ("Sentinela 007
    Oculta"): custo ausente, venda abaixo do custo, variação de preço, estoque negativo e produtos que param
    ou começam a vender, para encher os três blocos."""
    meses = []
    for indice in range(24):
        ano, mes = 2024 + indice // 12, indice % 12 + 1
        linhas = []
        for p in range(produtos):
            if (p % 9 == 0 and ano == 2025) or (p % 11 == 0 and ano == 2024):
                continue
            quantidade = 3 + (p * 7 + indice * 5) % 17
            preco = round(4.5 + p * 1.37, 2)
            praticado = preco if (p + indice) % 6 else round(preco * 0.9, 2)
            total = round(quantidade * praticado, 2)
            custo = "" if p % 8 == 0 else f"{round(quantidade * preco * (1.08 if p % 13 == 0 else 0.7), 2):.2f}"
            estoque = -2 if (p % 10 == 0 and indice % 4 == 0) else 5
            linhas.append(f"{100 + p};Sentinela {p:03d} Oculta;{quantidade};{preco:.2f};{total:.2f};{custo};{estoque}"
                          .replace(".", ","))
        meses.append(_relatorio(ano, mes, linhas))
    return meses


class _PublicaCase(_BancoCase):
    def setUp(self):
        super().setUp()
        import blueprints.avaliacao_publica as publica
        # As vitrines guardadas e as tentativas do dia são do processo: cada teste começa do zero.
        publica.zerar_contadores()
        self.addCleanup(publica.zerar_contadores)
        self.app.config.update(AVALIACAO_PUBLICA_ATIVA=True, AVALIACAO_PUBLICA_TEMPO_MINIMO_SEGUNDOS=0,
                               WHATSAPP_EMPRESA_NUMERO=NUMERO)
        self.worker_chamado, self.limites = 0, None

        def worker(caminhos, limits=None):
            self.worker_chamado += 1
            self.limites = limits
            return avaliar_como_o_worker(caminhos, limits)

        self.stack.enter_context(patch("etl_runtime.run_avaliacao_cenario", side_effect=worker))
        self.visitante = self.app.test_client()

    def dados(self, **troca):
        base = {"nome": "Maria Fictícia", "empresa": EMPRESA, "email": "maria@example.invalid",
                "whatsapp": "(62) 98888-1234", "plano_interesse": "PRATA", "faixa_faturamento": "ATE_50K",
                "dificuldade": "Repor sem excesso", "autorizacao": "1"}
        base.update(troca)
        return {nome: valor for nome, valor in base.items() if valor is not None}

    @staticmethod
    def token(html):
        return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)

    def enviar(self, arquivos, cliente=None, **troca):
        cliente = cliente or self.visitante
        pagina = cliente.get(PAGINA)
        self.assertEqual(pagina.status_code, 200)
        dados = {"csrf_token": self.token(pagina.get_data(as_text=True)), **self.dados(**troca),
                 "arquivos": [(io.BytesIO(conteudo), nome) for nome, conteudo in arquivos]}
        return cliente.post(PAGINA, data=dados, content_type="multipart/form-data")

    @staticmethod
    def lote(conteudos=LOTE_MESMOS_MESES):
        return [(f"mes_{indice:02d}.csv", conteudo) for indice, conteudo in enumerate(conteudos, 1)]

    def leads(self):
        from models import LeadAvaliacao
        self.db.session.expire_all()
        return self.db.session.scalars(select(LeadAvaliacao)).all()

    def _migrar(self, comando, revisao):
        import flask_migrate
        from tests.deploy_support import ROOT
        self.db.session.commit()
        self.db.session.remove()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                patch("logging.config.fileConfig"):
            getattr(flask_migrate, comando)(directory=str(ROOT / "migrations"), revision=revisao)


# =============================================================================
# 1. Desligado por padrão
# =============================================================================
class DesligadaTests(_PublicaCase):
    def test_desligada_responde_404_e_a_landing_nao_leva_a_ela(self):
        self.app.config["AVALIACAO_PUBLICA_ATIVA"] = False
        token = self.csrf_token("/auth/login")
        for metodo, caminho in (("GET", PAGINA), ("POST", PAGINA), ("GET", RESULTADO), ("POST", SAIR)):
            with self.subTest(metodo=metodo, caminho=caminho):
                dados = {"csrf_token": token} if metodo == "POST" else None
                self.assertEqual(self.client.open(caminho, method=metodo, data=dados).status_code, 404)
        # Na landing (decisões do dono, 16/11/2026), o "REALIZAR RAIO-X" do pré-cadastro abre o envio dos
        # relatórios no próprio diálogo, para o microsserviço: é um botão, com o Raio-X público ligado ou não, e o
        # diálogo não tem destino local nenhum (um link para página inexistente reprovaria aqui).
        for ativa in (False, True):
            with self.subTest(ativa=ativa):
                self.app.config["AVALIACAO_PUBLICA_ATIVA"] = ativa
                html = self.client.get("/").get_data(as_text=True)
                dialogo = html.split('<dialog id="precadastro"', 1)[1].split("</dialog>", 1)[0]
                self.assertIn('<button type="button" id="btn-raiox-fazer"', dialogo)
                self.assertNotIn(PAGINA, html)
                # Na junção com os documentos legais, só as páginas públicas deles.
                locais = [l["attrs"]["href"] for l in _Pagina(dialogo).links if l["attrs"].get("href", "").startswith("/")
                          and l["attrs"]["href"] not in ("/termos-de-uso", "/politica-de-privacidade")]
                self.assertEqual(locais, [])

    def test_padrao_do_config_e_desligado(self):
        import config
        self.assertIs(config.Config.AVALIACAO_PUBLICA_ATIVA, False)


# =============================================================================
# 2. Envio e gravação
# =============================================================================
class EnvioTests(_PublicaCase):
    def test_formulario_publico_sem_cache_e_fora_da_busca(self):
        resposta = self.visitante.get(PAGINA)
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.headers["Cache-Control"], "no-store")
        self.assertIn("noindex", resposta.headers["X-Robots-Tag"])
        html = resposta.get_data(as_text=True)
        for trecho in ('name="csrf_token"', 'name="campo_isca"', 'name="autorizacao" value="1" required',
                       "Autorizo o uso destes relatórios nesta avaliação.", 'accept=".csv,.xlsx,.xls,.xml"',
                       '<option value="CUSTOM">Custom / Enterprise</option>', 'enctype="multipart/form-data"',
                       '<meta name="robots" content="noindex, nofollow"'):
            self.assertIn(trecho, html)
        dom = _Pagina(html)
        isca = next(attrs for tag, attrs in dom.elementos if attrs.get("name") == "campo_isca")
        self.assertEqual((isca.get("tabindex"), isca.get("autocomplete")), ("-1", "off"))
        self.assertEqual(dom.whatsapp(), [])

    def test_envio_grava_lead_do_site_sem_disparar_whatsapp(self):
        import blueprints.avaliacao_publica as publica
        # Mesmo com o campo que liga o aviso no fluxo do consultor: nada de WhatsApp nem de e-mail ao lead.
        with patch("whatsapp_service.notificador") as notificador, patch("emails.enviar_email") as email:
            resposta = self.enviar(self.lote(), aviso_whatsapp="1")
        self.assertEqual(resposta.status_code, 303, resposta.get_data(as_text=True)[:600])
        self.assertEqual(resposta.headers["Location"], RESULTADO)
        notificador.assert_not_called()
        email.assert_not_called()
        # O teto por arquivo do processo isolado é o do público (8 MB), não o do consultor.
        self.assertEqual(self.limites.max_file_bytes, 8 * 1024 * 1024)
        [lead] = self.leads()
        self.assertEqual((lead.origem, lead.registrado_por, lead.status), ("site", None, "concluido"))
        self.assertEqual((lead.nome_contato, lead.nome_loja, lead.email, lead.whatsapp),
                         ("Maria Fictícia", EMPRESA, "maria@example.invalid", "62988881234"))
        self.assertEqual((lead.plano_interesse, lead.faixa_faturamento, lead.dificuldade),
                         ("PRATA", "ATE_50K", "Repor sem excesso"))
        self.assertEqual(lead.versao_autorizacao, publica.VERSAO_AUTORIZACAO)
        self.assertIsNotNone(lead.consentimento_em)
        # O processo isolado com o prazo do público (o executor dobra: 60 s).
        self.assertEqual((self.worker_chamado, self.limites.timeout_seconds), (1, publica.PRAZO_DO_PROCESSO_SEGUNDOS))
        # A sessão guarda só o id e o instante.
        with self.visitante.session_transaction() as sessao:
            self.assertEqual(set(sessao[publica.CHAVE_DA_SESSAO]), {"lead", "em"})
            self.assertEqual(sessao[publica.CHAVE_DA_SESSAO]["lead"], str(lead.id))

    def test_resultado_mostra_a_vitrine_e_o_desbloqueio_pelo_whatsapp(self):
        import blueprints.avaliacao_publica as publica
        self.enviar(self.lote())
        [lead] = self.leads()
        resposta = self.visitante.get(RESULTADO)
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.headers["Cache-Control"], "no-store")
        html = resposta.get_data(as_text=True)
        for texto in ("Resultado do Raio-X", EMPRESA, f"protocolo {lead.protocolo}", "Urgência", "Atenção", "Eletiva",
                      "Potencial recuperável (estimativa)", "Leite 1L"):
            self.assertIn(texto, html)
        self.assertNotIn(str(lead.id), html)
        [desbloquear] = _Pagina(html).whatsapp()
        partes = urlsplit(desbloquear["attrs"]["href"])
        self.assertEqual((partes.netloc, partes.path), ("wa.me", "/" + NUMERO))
        mensagem = parse_qs(partes.query, strict_parsing=True)["text"][0]
        self.assertEqual(mensagem, publica.mensagem_de_desbloqueio(lead))
        self.assertEqual(mensagem.splitlines(), [
            "*NOVO CONTATO COMERCIAL | NEXO*", "", "Nome: Maria Fictícia", f"Empresa: {EMPRESA}",
            "WhatsApp: (62) 98888-1234", "Plano de interesse: Prata", "E-mail: maria@example.invalid",
            "Faixa de faturamento: Até R$ 50 mil", "Principal necessidade: Repor sem excesso", "",
            f"Situação do Raio-X: Realizado (protocolo {lead.protocolo})", "Solicitou auxílio: Não",
            "Origem: Landing Page da NEXO"])
        self.assertEqual((desbloquear["attrs"]["target"], desbloquear["attrs"]["rel"]), ("_blank", "noopener noreferrer"))
        self.assertEqual(desbloquear["nome"], f"Desbloquear Relatório Completo {NOVA_ABA}")
        # Sem número válido, o botão fica desabilitado com o texto de canal ainda não publicado.
        self.app.config["WHATSAPP_EMPRESA_NUMERO"] = ""
        html = self.visitante.get(RESULTADO).get_data(as_text=True)
        self.assertEqual(_Pagina(html).whatsapp(), [])
        self.assertIn('disabled aria-describedby="publica-desbloqueio-nota">Desbloquear Relatório Completo', html)

    def test_padroes_aprovados_pelo_dono(self):
        """Os números aprovados em 15/11/2026, fixados: mudar qualquer um exige mudar este teste."""
        import blueprints.avaliacao_publica as publica
        import etl_runtime  # noqa: F401  (o executor dobra o prazo: run_avaliacao_cenario)
        self.assertEqual(publica.PADROES, {"MAX_ARQUIVOS": 12, "MAX_MB": 8, "TETO_DIARIO": 20,
                                           "TEMPO_MINIMO_SEGUNDOS": 5, "RESULTADO_HORAS": 8})
        self.assertEqual((publica.LIMITE_DE_ENVIOS, publica.LIMITE_DE_LEITURAS), ("3 per hour;5 per day", "60 per hour"))
        # 30 s no limite; o executor da avaliação dobra: 60 s, metade dos 120 s do consultor.
        self.assertEqual(min(120, publica.PRAZO_DO_PROCESSO_SEGUNDOS * 2), 60)
        self.assertEqual(diagnostico_cenario.MINIMO_PARA_TOTAIS_DO_BLOCO, 3)
        self.assertEqual(diagnostico_cenario.BLOQUEADOS_POR_BLOCO, 3)

    def test_busca_por_protocolo_e_reenvio_do_consultor(self):
        from models import LeadAvaliacao
        self.assertEqual(self.enviar(self.lote()).status_code, 303)
        self.assertEqual(self.enviar(self.lote(), cliente=self.app.test_client(), empresa="Outra Loja Fictícia",
                                     email="outra@example.invalid").status_code, 303)
        primeiro, segundo = sorted(self.leads(), key=lambda lead: lead.nome_loja != EMPRESA)
        self.login("admin-test@example.invalid")
        for protocolo in (primeiro.protocolo, primeiro.protocolo.lower()):
            with self.subTest(protocolo=protocolo):
                html = self.client.get(f"/admin/leads?protocolo={protocolo}").get_data(as_text=True)
                self.assertIn(EMPRESA, html)
                self.assertNotIn("Outra Loja Fictícia", html)
                # Na busca, nada de "Mostrando as N mais recentes": a lista foi filtrada, não cortada.
                self.assertNotIn("Mostrando as", html)
        html = self.client.get("/admin/leads?protocolo=ZZZ").get_data(as_text=True)
        self.assertIn("Protocolo inválido", html)
        self.assertIn("Outra Loja Fictícia", html)
        # O reenvio do consultor: a autorização que vale passa a ser a dele, e o lead continua sendo do site. A
        # vitrine guardada do visitante sai junto (os dados de que ela veio foram trocados).
        import blueprints.avaliacao_publica as publica
        self.assertIsNotNone(publica._vitrine_guardada(primeiro.id))
        pagina = self.client.get(f"/admin/leads/{primeiro.id}").get_data(as_text=True)
        resposta = self.client.post(f"/admin/leads/{primeiro.id}/reenviar", content_type="multipart/form-data", data={
            "csrf_token": self.token(pagina), "autorizacao": "1",
            "arquivos": [(io.BytesIO(conteudo), nome) for nome, conteudo in self.lote()]})
        self.assertEqual(resposta.status_code, 303)
        self.assertIsNone(publica._vitrine_guardada(primeiro.id))
        self.db.session.expire_all()
        lead = self.db.session.get(LeadAvaliacao, primeiro.id)
        self.assertEqual((lead.origem, lead.registrado_por, lead.versao_autorizacao), ("site", self.admin_id, None))

    def test_consultor_ve_o_lead_do_site_com_o_que_o_visitante_informou(self):
        self.enviar(self.lote())
        [lead] = self.leads()
        self.login("admin-test@example.invalid")
        lista = self.client.get("/admin/leads").get_data(as_text=True)
        self.assertIn(f"protocolo {lead.protocolo}", lista)
        self.assertIn("Site</span>", lista)
        detalhe = self.client.get(f"/admin/leads/{lead.id}").get_data(as_text=True)
        for texto in ("Raio-X público", f"protocolo {lead.protocolo}", "plano de interesse: Prata",
                      "faturamento: Até R$ 50 mil", "Principal dificuldade: Repor sem excesso"):
            self.assertIn(texto, detalhe)


# =============================================================================
# 3. Vitrine por lista branca
# =============================================================================
class VitrineTests(_PublicaCase):
    def test_nenhum_produto_bloqueado_chega_ao_html(self):
        from blueprints.leads import _lead_para_payload
        from horario import agora_utc
        import avaliacao_cenario
        self.app.config.update(AVALIACAO_PUBLICA_MAX_ARQUIVOS=24)
        self.assertEqual(self.enviar(self.lote(lote_de_sentinelas())).status_code, 303)
        [lead] = self.leads()
        html = self.visitante.get(RESULTADO).get_data(as_text=True)
        # Gabarito: o diagnóstico completo do consultor, com todos os itens de cada bloco.
        linhas, _ = avaliacao_cenario.linhas_do_lead(lead.id)
        completo = diagnostico_cenario.montar_payload(_lead_para_payload(lead), linhas, agora=agora_utc(),
                                                      retencao_dias=30, limite=200)
        self.assertEqual(completo["situacao"], "atual")
        ocultos = 0
        for bloco, dados in completo["blocos"].items():
            itens = dados["itens"]
            with self.subTest(bloco=bloco):
                if itens:
                    self.assertIn(itens[0]["produto"], html)
                for item in itens[1:]:
                    ocultos += 1
                    self.assertIsNone(re.search(re.escape(item["produto"]) + r"(?!\d)", html), item["produto"])
                    if item["codigo"]:
                        self.assertNotIn(f">{item['codigo']}<", html)
                bloqueados = min(max(dados["total"] - 1, 0), diagnostico_cenario.BLOQUEADOS_POR_BLOCO)
                trecho = html.split(f'id="publica-bloco-{bloco}"', 1)[1].split("</section>", 1)[0]
                self.assertEqual(trecho.count('class="publica-bloqueado"'), bloqueados)
                self.assertEqual("Faturamento R$" in trecho,
                                 dados["total"] >= diagnostico_cenario.MINIMO_PARA_TOTAIS_DO_BLOCO)
        self.assertGreater(ocultos, 6)
        # O texto fixo das linhas bloqueadas, e nada de dado do lead em atributo, script ou comentário.
        self.assertIn("Conteúdo bloqueado para leads", html)
        self.assertNotIn("<!--", html)
        self.assertNotIn("data-texto", html)

    def test_chaves_da_vitrine_sao_exatamente_as_da_lista_branca(self):
        from blueprints.leads import _lead_para_payload
        from horario import agora_utc
        import avaliacao_cenario
        self.enviar(self.lote())
        [lead] = self.leads()
        linhas, _ = avaliacao_cenario.linhas_do_lead(lead.id)
        vitrine = diagnostico_cenario.visao_publica(_lead_para_payload(lead), linhas, agora=agora_utc(),
                                                    retencao_dias=30)
        self.assertEqual(set(vitrine), {"versao", "situacao", "blocos", "potencial_recuperavel_total"})
        self.assertEqual([b["bloco"] for b in vitrine["blocos"]], ["urgencia", "atencao", "eletiva"])
        for bloco in vitrine["blocos"]:
            self.assertEqual(set(bloco), {"bloco", "rotulo", "total", "real", "ocultos", "bloqueados",
                                          "faturamento", "recuperavel"})
            if bloco["real"] is not None:
                self.assertEqual(set(bloco["real"]), {"produto", "categoria", "motivos", "impacto", "a_confirmar"})
                self.assertTrue(all(isinstance(motivo, str) for motivo in bloco["real"]["motivos"]))

    def _vitrine_com(self, totais):
        """A vitrine de um lead fictício com `totais` produtos por bloco (urgência, atenção, eletiva)."""
        from horario import agora_utc
        agora = agora_utc()
        lead_id = uuid.uuid4()

        def resumo(itens):
            return json.dumps({"versao": "x", "itens": itens, "faturamento": "1000.00", "recuperavel": "50.00",
                               "motivos": {}})

        def item(nome):
            return {"produto": nome, "categoria": None, "motivos": [{"codigo": "m", "rotulo": "Motivo"}],
                    "impacto": 10.0, "a_confirmar": False, "codigo": "SEGREDO", "faturamento": 999.0}
        nomes = ("urgencia", "atencao", "eletiva")
        lead = {"id": lead_id, "status": "concluido", "vazamento_total_reais": Decimal("123.45"),
                "resumos_json": {bloco: resumo(total) for bloco, total in zip(nomes, totais)}}
        linhas = [{"lead_id": lead_id, "versao_regras": avaliacao_motor.VERSAO_REGRAS_LEAD, "criado_em": agora}]
        itens = {bloco: {"total": total, "itens": [item(f"Real {bloco}")] if total else []}
                 for bloco, total in zip(nomes, totais)}
        with patch.object(diagnostico_cenario, "_triagem_do_lead", return_value=(None, None, None, None)), \
                patch.object(diagnostico_cenario, "itens_dos_blocos", return_value=itens) as montar:
            vitrine = diagnostico_cenario.visao_publica(lead, linhas, agora=agora, retencao_dias=30)
        self.assertEqual(montar.call_args.kwargs["limite"], 1)
        self.assertNotIn("SEGREDO", json.dumps(vitrine))
        return {b["bloco"]: b for b in vitrine["blocos"]}, vitrine, lead, linhas, agora

    def test_regras_da_vitrine_por_quantidade_de_produtos(self):
        """Blocos com 0, 1, 2, 3, 4 e 5 produtos: as linhas bloqueadas (até 3) e o R$ do bloco só com 3 ou mais
        (o limiar dos dois lados: com 2 não sai, com 3 sai)."""
        esperado = {0: (None, 0, 0, False), 1: ("Real", 0, 0, False), 2: ("Real", 1, 1, False),
                    3: ("Real", 2, 2, True), 4: ("Real", 3, 3, True), 5: ("Real", 4, 3, True)}
        for totais in ((0, 2, 5), (1, 3, 4)):
            blocos, vitrine, lead, linhas, agora = self._vitrine_com(totais)
            for bloco in blocos.values():
                total = bloco["total"]
                with self.subTest(total=total):
                    real, ocultos, bloqueados, com_reais = esperado[total]
                    self.assertEqual(bool(bloco["real"]), real is not None)
                    self.assertEqual((bloco["ocultos"], bloco["bloqueados"]), (ocultos, bloqueados))
                    self.assertEqual(bloco["faturamento"] is not None, com_reais)
                    self.assertEqual(bloco["recuperavel"] is not None, com_reais)
                    if com_reais:
                        self.assertEqual((bloco["faturamento"], bloco["recuperavel"]), (1000.0, 50.0))
            self.assertEqual(vitrine["potencial_recuperavel_total"], 123.45)
        self.assertEqual(blocos["urgencia"]["real"], {"produto": "Real urgencia", "categoria": None,
                                                      "motivos": ["Motivo"], "impacto": 10.0, "a_confirmar": False})
        # Linha de outro lead é recusada, como no payload do consultor.
        with self.assertRaises(ValueError):
            diagnostico_cenario.visao_publica(lead, [{**linhas[0], "lead_id": uuid.uuid4()}], agora=agora,
                                              retencao_dias=30)

    def test_resultado_vem_da_vitrine_guardada_sem_tocar_no_banco(self):
        """Calculada uma vez, no envio: o GET do resultado só desenha. Depois de um reinício (sem a vitrine), o GET
        refaz uma vez e guarda; enquanto outro pedido refaz, a resposta é 503 com Retry-After."""
        import blueprints.avaliacao_publica as publica
        self.enviar(self.lote())
        consultas = []

        def ouvir(_conexao, _cursor, comando, _parametros, _contexto, _varios):
            consultas.append(comando[:80])

        event.listen(self.db.engine, "before_cursor_execute", ouvir)
        self.addCleanup(event.remove, self.db.engine, "before_cursor_execute", ouvir)
        primeira = self.visitante.get(RESULTADO).get_data(as_text=True)
        self.assertIn("Leite 1L", primeira)
        self.assertEqual([c for c in consultas if "lead_avaliacao" in c or "brainstorming_dados_pdv" in c], [])
        # Reinício do processo: sem a vitrine guardada, um pedido por vez refaz.
        publica.zerar_contadores()
        self.assertTrue(publica._recalculo_trava.acquire(blocking=False))
        try:
            resposta = self.visitante.get(RESULTADO)
        finally:
            publica._recalculo_trava.release()
        self.assertEqual((resposta.status_code, resposta.headers["Retry-After"]), (503, "5"))
        self.assertIn("está sendo preparado", resposta.get_data(as_text=True))
        consultas.clear()
        refeita = self.visitante.get(RESULTADO).get_data(as_text=True)
        self.assertTrue(any("brainstorming_dados_pdv" in c for c in consultas))
        # A mesma página (o token assinado do CSRF muda com o instante: fica de fora da comparação).
        sem_token = lambda html: re.sub(r'name="csrf_token" value="[^"]+"', "", html)  # noqa: E731
        self.assertEqual(sem_token(refeita), sem_token(primeira))
        consultas.clear()
        self.assertEqual(self.visitante.get(RESULTADO).status_code, 200)
        self.assertEqual([c for c in consultas if "lead_avaliacao" in c or "brainstorming_dados_pdv" in c], [])

    def test_descarte_durante_o_calculo_nao_guarda_a_vitrine_de_antes(self):
        """O consultor reenvia ou refaz a triagem enquanto o GET recalcula a vitrine (processo que acabou de
        acordar): a vitrine calculada com o dado de antes é desenhada uma vez, mas não fica guardada; o GET
        seguinte refaz com o dado atual (como a geração do cache do diagnóstico)."""
        import blueprints.avaliacao_publica as publica
        from blueprints.leads import _descartar_do_cache
        self.enviar(self.lote())
        [lead] = self.leads()
        publica.zerar_contadores()
        original = diagnostico_cenario.visao_publica

        def com_descarte_no_meio(*args, **kwargs):
            vitrine = original(*args, **kwargs)
            _descartar_do_cache(lead.id)
            return vitrine

        with patch("diagnostico_cenario.visao_publica", side_effect=com_descarte_no_meio):
            self.assertEqual(self.visitante.get(RESULTADO).status_code, 200)
        self.assertIsNone(publica._vitrine_guardada(lead.id))
        self.assertEqual(self.visitante.get(RESULTADO).status_code, 200)
        self.assertIsNotNone(publica._vitrine_guardada(lead.id))

    def test_nome_de_produto_e_escapado(self):
        vitrine = {"versao": "x", "situacao": "atual", "potencial_recuperavel_total": None,
                   "blocos": [{"bloco": "urgencia", "rotulo": "Urgência", "total": 1, "ocultos": 0, "bloqueados": 0,
                               "faturamento": None, "recuperavel": None,
                               "real": {"produto": '<script>window.X=1</script>', "categoria": '<img src=x onerror=1>',
                                        "motivos": ["<b>m</b>"], "impacto": 1.0, "a_confirmar": False}}]}
        with self.app.test_request_context(RESULTADO):
            html = render_template("avaliacao_publica/resultado.html", vitrine=vitrine, empresa="<i>Loja</i>",
                                   protocolo="ABCD1234", mensagem="x")
        self.assertNotIn("<script>window", html)
        self.assertNotIn("<img src=x", html)
        self.assertNotIn("<b>m</b>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&lt;i&gt;Loja&lt;/i&gt;", html)


# =============================================================================
# 4. O resultado é da sessão de quem enviou
# =============================================================================
class SessaoTests(_PublicaCase):
    def test_outro_navegador_nao_ve_o_resultado(self):
        self.enviar(self.lote())
        for cliente, rotulo in ((self.app.test_client(), "visitante novo"), (self.client, "cliente do portal")):
            with self.subTest(cliente=rotulo):
                if rotulo == "cliente do portal":
                    self.login()
                resposta = cliente.get(RESULTADO)
                self.assertEqual(resposta.status_code, 303)
                self.assertEqual(resposta.headers["Location"], PAGINA)
                self.assertNotIn(EMPRESA, cliente.get(PAGINA).get_data(as_text=True))

    def test_validade_e_encerrar(self):
        import blueprints.avaliacao_publica as publica
        self.enviar(self.lote())
        self.assertEqual(self.visitante.get(RESULTADO).status_code, 200)
        # Vencida (mais de 8 horas): volta ao formulário e a chave sai da sessão.
        with self.visitante.session_transaction() as sessao:
            sessao[publica.CHAVE_DA_SESSAO] = {**sessao[publica.CHAVE_DA_SESSAO], "em": int(time.time()) - 9 * 3600}
        self.assertEqual(self.visitante.get(RESULTADO).status_code, 303)
        with self.visitante.session_transaction() as sessao:
            self.assertNotIn(publica.CHAVE_DA_SESSAO, sessao)
        # Encerrar: o POST com o token apaga o vínculo e tira a vitrine da memória.
        self.enviar(self.lote(), email="outra@example.invalid")
        html = self.visitante.get(RESULTADO).get_data(as_text=True)
        segundo = next(lead for lead in self.leads() if lead.email == "outra@example.invalid")
        self.assertIsNotNone(publica._vitrine_guardada(segundo.id))
        resposta = self.visitante.post(SAIR, data={"csrf_token": self.token(html)})
        self.assertEqual((resposta.status_code, resposta.headers["Location"]), (303, "/"))
        self.assertIsNone(publica._vitrine_guardada(segundo.id))
        self.assertEqual(self.visitante.get(RESULTADO).status_code, 303)

    def test_vitrines_vencidas_saem_da_memoria_a_cada_pedido(self):
        """A vitrine de quem não voltou não espera uma leitura da mesma chave: qualquer pedido ao processo tira as
        vencidas (o contato e os produtos não ficam guardados além das 8 horas)."""
        import blueprints.avaliacao_publica as publica
        self.enviar(self.lote())
        [lead] = self.leads()
        self.assertIn(lead.id, publica._vitrines)
        _validade, contexto = publica._vitrines[lead.id]
        publica._vitrines[lead.id] = (time.monotonic() - 1, contexto)
        self.app.test_client().get("/")
        self.assertNotIn(lead.id, publica._vitrines)

    def test_formulario_sem_marca_nao_prende_em_laco(self):
        """Uma aba aberta antes de um envio concluído (a marca do formulário saiu da sessão): a recusa devolve a
        marca, e o reenvio da página devolvida passa."""
        import blueprints.avaliacao_publica as publica
        pagina = self.visitante.get(PAGINA).get_data(as_text=True)
        with self.visitante.session_transaction() as sessao:
            sessao.pop(publica.CHAVE_DO_FORMULARIO)
        dados = lambda: {"csrf_token": self.token(pagina), **self.dados(),  # noqa: E731
                         "arquivos": [(io.BytesIO(conteudo), nome) for nome, conteudo in self.lote()]}
        resposta = self.visitante.post(PAGINA, data=dados(), content_type="multipart/form-data")
        self.assertEqual(resposta.status_code, 422)
        self.assertIn("O formulário expirou", resposta.get_data(as_text=True))
        resposta = self.visitante.post(PAGINA, data=dados(), content_type="multipart/form-data")
        self.assertEqual(resposta.status_code, 303)

    def test_sessao_forjada_ou_de_lead_do_consultor_nao_abre(self):
        import avaliacao_cenario
        import blueprints.avaliacao_publica as publica
        from tests.raio_x_support import avaliar_como_o_worker as worker
        caminhos = self.caminhos(*LOTE_MESMOS_MESES)
        consultor = avaliacao_cenario.registrar(worker(caminhos), contato={
            "nome_contato": "Contato do consultor", "email": "consultor-lead@example.invalid", "whatsapp": None,
            "nome_loja": "Loja do Consultor"}, registrado_por=self.admin_id, meses_prata=24)
        self.db.session.commit()
        for valor in ({"lead": str(consultor.id), "em": int(time.time())}, {"lead": "x", "em": int(time.time())},
                      {"lead": str(uuid.uuid4()), "em": True}, "texto"):
            with self.subTest(valor=valor):
                with self.visitante.session_transaction() as sessao:
                    sessao[publica.CHAVE_DA_SESSAO] = valor
                resposta = self.visitante.get(RESULTADO)
                self.assertEqual(resposta.status_code, 303)
                self.assertNotIn("Loja do Consultor", self.visitante.get(PAGINA).get_data(as_text=True))


# =============================================================================
# 5. Pedido inválido, tetos e limites
# =============================================================================
class RecusasTests(_PublicaCase):
    def test_pedido_invalido_nao_le_arquivo_nao_grava_e_nao_toca_nas_tabelas_de_lead(self):
        consultas = []

        def ouvir(_conexao, _cursor, comando, _parametros, _contexto, _varios):
            if "lead_avaliacao" in comando or "brainstorming_dados_pdv" in comando:
                consultas.append(comando[:80])

        event.listen(self.db.engine, "before_cursor_execute", ouvir)
        self.addCleanup(event.remove, self.db.engine, "before_cursor_execute", ouvir)
        treze = [(f"m{i}.csv", b"x") for i in range(13)]
        casos = {
            "sem autorização": (self.lote(), {"autorizacao": None}, 422),
            "e-mail inválido": (self.lote(), {"email": "sem-arroba"}, 422),
            "sem WhatsApp": (self.lote(), {"whatsapp": ""}, 422),
            "plano fora da lista": (self.lote(), {"plano_interesse": "PLATINA"}, 422),
            "faixa fora da lista": (self.lote(), {"faixa_faturamento": "MILHAO"}, 422),
            "dificuldade longa": (self.lote(), {"dificuldade": "x" * 401}, 422),
            "sem empresa": (self.lote(), {"empresa": "   "}, 422),
            "campo isca": (self.lote(), {"campo_isca": "http://spam.example.invalid"}, 422),
            "sem arquivos": ([], {}, 422),
            "13 arquivos": (treze, {}, 422),
            # Antes do esquema, da vaga e do teto (que lê a tabela de lead): o formato errado não ocupa a vaga.
            "formato": ([("relatorio.pdf", b"%PDF-1.4 ficticio")], {}, 422),
            "WhatsApp que viraria outro número": (self.lote(), {"whatsapp": "(55) 99276-54321"}, 422),
        }
        for rotulo, (arquivos, troca, status) in casos.items():
            with self.subTest(caso=rotulo):
                resposta = self.enviar(arquivos, **troca)
                self.assertEqual(resposta.status_code, status, resposta.get_data(as_text=True)[:400])
                self.assertEqual(resposta.headers["Cache-Control"], "no-store")
        # Os campos digitados voltam, menos a autorização (marca-se a cada envio).
        html = self.enviar(self.lote(), autorizacao=None).get_data(as_text=True)
        self.assertIn(f'value="{EMPRESA}"', html)
        self.assertNotIn("checked", html.split('name="autorizacao"', 1)[1].split(">", 1)[0])
        # Tempo mínimo desde a abertura do formulário.
        self.app.config["AVALIACAO_PUBLICA_TEMPO_MINIMO_SEGUNDOS"] = 60
        self.assertEqual(self.enviar(self.lote()).status_code, 422)
        self.app.config["AVALIACAO_PUBLICA_TEMPO_MINIMO_SEGUNDOS"] = 0
        # Corpo acima do teto do público (1 MB aqui): 413, sem ler arquivo.
        self.app.config["AVALIACAO_PUBLICA_MAX_MB"] = 1
        self.assertEqual(self.enviar([("grande.csv", b"x" * (1024 * 1024 + 10))]).status_code, 413)
        self.assertEqual(self.worker_chamado, 0)
        # Antes da contagem abaixo (que é, ela mesma, uma consulta à tabela de lead).
        self.assertEqual(consultas, [])
        self.assertEqual(self.leads(), [])

    def test_teto_diario_e_vaga_ocupada(self):
        self.app.config["AVALIACAO_PUBLICA_TETO_DIARIO"] = 1
        self.assertEqual(self.enviar(self.lote()).status_code, 303)
        resposta = self.enviar(self.lote(), cliente=self.app.test_client(), email="segunda@example.invalid")
        self.assertEqual(resposta.status_code, 429)
        self.assertIn("limite de avaliações pelo site por hoje", resposta.get_data(as_text=True))
        self.assertEqual((self.worker_chamado, len(self.leads())), (1, 1))
        # Vaga única ocupada (envio do consultor, do cliente ou do Raio-X): 503 com Retry-After, sem fila.
        self.app.config["AVALIACAO_PUBLICA_TETO_DIARIO"] = 20

        @contextlib.contextmanager
        def ocupada():
            yield False
        with patch("blueprints.avaliacao_publica.vaga_de_entrada", ocupada):
            resposta = self.enviar(self.lote(), cliente=self.app.test_client(), email="terceira@example.invalid")
        self.assertEqual(resposta.status_code, 503)
        self.assertEqual(resposta.headers["Retry-After"], "30")
        self.assertEqual(self.worker_chamado, 1)

    def test_relatorio_recusado_nao_grava(self):
        resposta = self.enviar([("vazio.csv", b"Relatorio sem periodo\nCodigo;Produto\n1;Nada\n")])
        self.assertEqual(resposta.status_code, 422)
        self.assertEqual(self.leads(), [])

    def test_formato_e_baia_recusados_nao_gastam_o_teto(self):
        """O teto conta só a tentativa que chega ao processo isolado: o formato errado (recusado antes da vaga) e a
        recusa da Baia (no processo web, antes do executor) não o gastam."""
        self.app.config["AVALIACAO_PUBLICA_TETO_DIARIO"] = 1
        resposta = self.enviar([("relatorio.pdf", b"%PDF-1.4 ficticio")])
        self.assertEqual(resposta.status_code, 422)
        self.assertIn("formato não suportado", resposta.get_data(as_text=True))
        # Texto com extensão de planilha: a Baia recusa pela assinatura, antes do processo isolado.
        resposta = self.enviar([("disfarce.xls", b"Codigo;Produto\n1;Nada\n")], cliente=self.app.test_client())
        self.assertEqual(resposta.status_code, 422)
        self.assertEqual(self.worker_chamado, 0)
        resposta = self.enviar(self.lote(), cliente=self.app.test_client(), email="valido@example.invalid")
        self.assertEqual(resposta.status_code, 303)
        self.assertEqual(self.worker_chamado, 1)

    def test_whatsapp_so_brasileiro_com_ddd(self):
        """A regra do envio pela Evolution (whatsapp_service.higienizar_telefone), mais estrita no que é ambíguo,
        como a máscara dos scripts: o número nunca vira outro. Sem "+" ou "00", o 55 do começo não vira DDI; com
        eles, só o DDI 55 passa; o DDD 55 do Rio Grande do Sul fica; ramal, letra, número de exemplo, DDD fora de
        uso e celular sem o nono dígito são recusados."""
        import blueprints.avaliacao_publica as publica
        from configuracoes_conta import DadoInvalido
        aceitos = {
            "+55 (62) 98888-1234": "62988881234", "0055 62 98888-1234": "62988881234",
            "+55 (062) 98888-1234": "62988881234", "0 62 98888-1234": "62988881234",
            "(62) 98888-1234": "62988881234", "(62) 3222-1234": "6232221234", "62.3222.1234": "6232221234",
            "(55) 99999-1234": "55999991234", "55 99999-1234": "55999991234", "+55 55 99999-1234": "55999991234",
            "(55) 3222-1234": "5532221234",
            # Colado do Contatos do Mac (marcas de direção invisíveis), com hífen tipográfico ou com o 0xx.
            "\u202d+55 (62) 98888-1234\u202c": "62988881234",
            "(62) 98888\u20111234": "62988881234", "(62)\u200b98888\u20131234": "62988881234",
            "(0xx62) 3222-1234": "6232221234",
        }
        for valor, esperado in aceitos.items():
            with self.subTest(aceito=valor):
                self.assertEqual(publica._whatsapp(valor), esperado)
        recusados = (
            "(55) 99276-54321", "559927654321", "5562988881234",   # 12 ou 13 dígitos sem "+": não viram outro DDD
            "+55 99999-8877", "0055 99999-8877",                    # DDI sem DDD
            "+1 202 555 0100", "+51 987 654 321", "+7 991 123-45-67",  # estrangeiros
            "(55) 3222-1234 ramal 21", "(55) 3222-1234 21", "62 3222-1234 / 62 99999-8877", "Tel: (62) 98888-1234",
            "3222-1234 21", "98917-1218 (99)",                     # ramal ou DDD depois: viraria outro número
            "(62) 9888-1234", "(62) 99999-9999", "(20) 98888-1234", "0 15 62 98888-1234", "123", "6" * 61,
        )
        for valor in recusados:
            with self.subTest(recusado=valor), self.assertRaises(DadoInvalido) as erro:
                publica._whatsapp(valor)
            self.assertEqual(str(erro.exception), publica.MENSAGEM_WHATSAPP)
        # Pela rota: gravado só com DDD e número; recusado com a mensagem do WhatsApp, sem gravar.
        self.assertEqual(self.enviar(self.lote(), whatsapp="+55 (62) 98888-1234").status_code, 303)
        [lead] = self.leads()
        self.assertEqual(lead.whatsapp, "62988881234")
        resposta = self.enviar(self.lote(), cliente=self.app.test_client(), whatsapp="(55) 99276-54321",
                               email="outro@example.invalid")
        self.assertEqual(resposta.status_code, 422)
        self.assertIn(publica.MENSAGEM_WHATSAPP, resposta.get_data(as_text=True))
        self.assertEqual(len(self.leads()), 1)

    def test_teto_conta_os_leads_do_banco_depois_de_reiniciar(self):
        """Depois de um reinício (o Render free dorme), as tentativas guardadas na memória zeram: o teto continua
        valendo pelos leads do site gravados hoje no banco."""
        import blueprints.avaliacao_publica as publica
        self.app.config["AVALIACAO_PUBLICA_TETO_DIARIO"] = 1
        self.assertEqual(self.enviar(self.lote()).status_code, 303)
        publica.zerar_contadores()
        resposta = self.enviar(self.lote(), cliente=self.app.test_client(), email="depois@example.invalid")
        self.assertEqual(resposta.status_code, 429)
        self.assertIn("limite de avaliações pelo site por hoje", resposta.get_data(as_text=True))
        self.assertEqual((self.worker_chamado, len(self.leads())), (1, 1))

    def test_envelope_do_formulario_nao_conta_no_teto(self):
        """O Content-Length conta também as fronteiras e os campos do formulário: arquivos dentro do teto passam
        mesmo com o corpo um pouco acima dele; acima da folga, 413 sem ler arquivo."""
        import blueprints.avaliacao_publica as publica
        self.app.config["AVALIACAO_PUBLICA_MAX_MB"] = 1
        teto = 1024 * 1024
        with patch("blueprints.avaliacao_publica._tamanho_declarado", return_value=teto + 4096):
            self.assertEqual(self.enviar(self.lote()).status_code, 303)
        with patch("blueprints.avaliacao_publica._tamanho_declarado",
                   return_value=teto + publica.FOLGA_DO_ENVELOPE + 1):
            resposta = self.enviar(self.lote(), cliente=self.app.test_client(), email="outro@example.invalid")
        self.assertEqual(resposta.status_code, 413)
        self.assertEqual(self.worker_chamado, 1)

    def test_tentativas_que_falham_contam_no_teto(self):
        """O envio que chega ao processo isolado e falha não grava lead, mas gasta o teto do dia: senão, envios
        ruins ocupariam a vaga única sem limite."""
        self.app.config["AVALIACAO_PUBLICA_TETO_DIARIO"] = 1
        ruim = [("vazio.csv", b"Relatorio sem periodo\nCodigo;Produto\n1;Nada\n")]
        self.assertEqual(self.enviar(ruim).status_code, 422)
        self.assertEqual(self.leads(), [])
        resposta = self.enviar(self.lote(), cliente=self.app.test_client(), email="depois@example.invalid")
        self.assertEqual(resposta.status_code, 429)
        self.assertEqual((self.worker_chamado, self.leads()), (1, []))

    def test_tamanho_real_dos_arquivos_vale_sem_content_length(self):
        """Corpo chunked não traz Content-Length: o teto do público vale pelo tamanho real dos arquivos."""
        self.app.config["AVALIACAO_PUBLICA_MAX_MB"] = 1
        with patch("blueprints.avaliacao_publica._tamanho_declarado", return_value=None):
            resposta = self.enviar([("grande.csv", b"x" * (1024 * 1024 + 10))])
        self.assertEqual(resposta.status_code, 413)
        self.assertIn("passam de 1 MB somados", resposta.get_data(as_text=True))
        self.assertEqual(self.worker_chamado, 0)

    def test_dia_do_teto_e_o_de_brasilia(self):
        """Às 23h30 de Brasília (02h30 UTC do dia seguinte), o lead gravado às 23h ainda é de hoje; à 0h30 de
        Brasília, não é mais."""
        from datetime import datetime, timezone
        import blueprints.avaliacao_publica as publica
        from models import LeadAvaliacao
        self.db.session.add(LeadAvaliacao(nome_contato="Contato", email="noite@example.invalid", nome_loja="Loja",
                                          origem="site", status="concluido",
                                          criado_em=datetime(2026, 11, 16, 2, 0, tzinfo=timezone.utc)))
        self.db.session.commit()
        with self.app.test_request_context(PAGINA):
            for agora, esperado in ((datetime(2026, 11, 16, 2, 30, tzinfo=timezone.utc), 1),
                                    (datetime(2026, 11, 16, 3, 30, tzinfo=timezone.utc), 0)):
                with self.subTest(agora=agora), patch("blueprints.avaliacao_publica.agora_utc", return_value=agora):
                    self.assertEqual(publica._avaliacoes_de_hoje(), esperado)


class LimitePorIpTests(_PublicaCase):
    def setUp(self):
        super().setUp()
        from extensions import limiter
        self.app.config["RATELIMIT_ENABLED"] = True
        limiter.init_app(self.app)
        self.addCleanup(limiter.reset)

    def test_tres_envios_por_hora_por_ip(self):
        status = [self.enviar([], **{"autorizacao": None}).status_code for _ in range(4)]
        self.assertEqual(status, [422, 422, 422, 429])
        # Outro IP tem a própria cota.
        outro = self.app.test_client()
        outro.environ_base["REMOTE_ADDR"] = "203.0.113.7"
        self.assertEqual(self.enviar([], cliente=outro, autorizacao=None).status_code, 422)

    def test_chave_por_ip_sem_o_ip_e_ipv6_pela_rede(self):
        from extensions import chave_limite_ip

        def chave(endereco):
            with self.app.test_request_context(PAGINA, environ_base={"REMOTE_ADDR": endereco}):
                return chave_limite_ip()
        self.assertTrue(chave("198.51.100.4").startswith("ip:"))
        self.assertNotIn("198.51.100.4", chave("198.51.100.4"))
        self.assertEqual(chave("2001:db8:1:2::1"), chave("2001:db8:1:2:ffff::9"))
        self.assertNotEqual(chave("2001:db8:1:2::1"), chave("2001:db8:1:3::1"))
        self.assertEqual(chave("::ffff:198.51.100.4"), chave("198.51.100.4"))
        self.assertNotEqual(chave("198.51.100.4"), chave("198.51.100.5"))


# =============================================================================
# 6. Migração e7a1c3f5b9d2
# =============================================================================
class MigracaoTests(_PublicaCase):
    def _sem_cache_do_esquema(self):
        from blueprints import leads
        leads._esquema_por_engine.clear()

    def test_downgrade_recusa_com_lead_do_site(self):
        self.assertEqual(self.enviar(self.lote()).status_code, 303)
        with self.assertLogs("flask_migrate", "ERROR") as registro, self.assertRaises(SystemExit):
            self._migrar("downgrade", "d9e4b7a2c6f1")
        self.assertIn("Downgrade da captação pelo site recusado", registro.output[0])
        self.assertEqual(len(self.leads()), 1)

    def test_sem_a_migracao_as_telas_avisam_e_depois_voltam(self):
        from sqlalchemy import inspect
        self._migrar("downgrade", "d9e4b7a2c6f1")
        self.addCleanup(self._sem_cache_do_esquema)
        self._sem_cache_do_esquema()
        colunas = {c["name"] for c in inspect(self.db.engine).get_columns("lead_avaliacao")}
        self.assertFalse({"origem", "plano_interesse", "faixa_faturamento", "dificuldade",
                          "versao_autorizacao"} & colunas)
        # O formulário não consulta o banco; o envio avisa (503) sem ler arquivo nem quebrar.
        self.assertEqual(self.visitante.get(PAGINA).status_code, 200)
        resposta = self.enviar(self.lote())
        self.assertEqual(resposta.status_code, 503)
        self.assertIn("indisponível no momento", resposta.get_data(as_text=True))
        self.assertEqual(self.worker_chamado, 0)
        self.login("admin-test@example.invalid")
        self.assertIn("e7a1c3f5b9d2", self.client.get("/admin/leads").get_data(as_text=True))
        self._migrar("upgrade", "head")
        self._sem_cache_do_esquema()
        self.assertEqual(self.enviar(self.lote()).status_code, 303)


# =============================================================================
# 7. No navegador real (Edge/Chromium via Playwright; NEXO_TEST_BROWSER=1)
# =============================================================================
_MEDIR = """() => {
    const vw = innerWidth;
    const fora = [...document.querySelectorAll('a, button, input, select, textarea, h1, h2')].filter(el => {
        const b = el.getBoundingClientRect(), cs = getComputedStyle(el);
        return b.width && cs.display !== 'none' && cs.visibility !== 'hidden' && !el.closest('.campo-isca')
            && (b.left < -1 || b.right > vw + 1);
    }).map(el => el.tagName.toLowerCase() + ' "' + (el.textContent || el.name || '').trim().slice(0, 40) + '"');
    return {rola: document.documentElement.scrollWidth > vw + 1, fora, csp: window.__cspViolations || []};
}"""


@unittest.skipUnless(_BROWSER, "Defina NEXO_TEST_BROWSER=1 e instale o Playwright para os testes de navegador.")
class RaioXPublicoNavegadorTests(NavegadorCase):
    def setUp(self):
        super().setUp()
        self.app.config.update(AVALIACAO_PUBLICA_ATIVA=True, AVALIACAO_PUBLICA_TEMPO_MINIMO_SEGUNDOS=0,
                               WHATSAPP_EMPRESA_NUMERO=NUMERO)
        self.stack.enter_context(patch("etl_runtime.run_avaliacao_cenario",
                                       side_effect=lambda caminhos, limits=None: avaliar_como_o_worker(caminhos, limits)))
        pasta = tempfile.TemporaryDirectory(prefix="nexo-raio-x-publico-")
        self.addCleanup(pasta.cleanup)
        self.arquivos = []
        for indice, conteudo in enumerate(LOTE_MESMOS_MESES, 1):
            caminho = Path(pasta.name) / f"vendas_{indice:02d}.csv"
            caminho.write_bytes(conteudo)
            self.arquivos.append(str(caminho))

    def _sem_cortes(self, pagina, contexto):
        medida = pagina.evaluate(_MEDIR)
        self.assertFalse(medida["rola"], f"{contexto}: a página rola na horizontal")
        self.assertEqual(medida["fora"], [], f"{contexto}: controle fora da tela")
        self.assertEqual(medida["csp"], [], f"{contexto}: violação de CSP")

    def test_do_pre_cadastro_ao_resultado(self):
        import blueprints.avaliacao_publica as publica
        for largura, altura in ((320, 640), (1366, 768)):
            with self.subTest(largura=largura):
                pagina = self.abrir(largura, altura)
                pagina.add_init_script(_COLETOR_CSP)
                # Desde 16/11/2026 o pré-cadastro da landing envia os relatórios ao microsserviço no próprio
                # diálogo e não leva mais a esta página. O que ela lê do sessionStorage ('nexo.precadastro',
                # static/js/avaliacao-publica.js) é posto aqui à mão, no formato que a landing gravava.
                self.ir(pagina, "/")
                pagina.evaluate("""() => sessionStorage.setItem('nexo.precadastro', JSON.stringify({
                    nome: 'Maria Fictícia', empresa: %s, whatsapp: '(62) 98888-1234', plano: 'Prata', email: '',
                    faturamento: 'Até R$ 50 mil', desafio: 'Repor sem excesso'}))""" % json.dumps(EMPRESA))
                self.ir(pagina, PAGINA)
                self.assertTrue(pagina.url.endswith(PAGINA), pagina.url)
                # O formulário público vem preenchido com o pré-cadastro, e o dado sai do sessionStorage.
                self.assertEqual([pagina.input_value(f"#publica-{campo}") for campo in ("nome", "empresa", "whatsapp")],
                                 ["Maria Fictícia", EMPRESA, "(62) 98888-1234"])
                self.assertEqual((pagina.input_value("#publica-plano"), pagina.input_value("#publica-faixa")),
                                 ("PRATA", "ATE_50K"))
                self.assertIsNone(pagina.evaluate("sessionStorage.getItem('nexo.precadastro')"))
                self._sem_cortes(pagina, f"formulário @ {largura}px")
                pagina.fill("#publica-email", f"maria-{largura}@example.invalid")
                pagina.set_input_files("#publica-arquivos", self.arquivos)
                pagina.check("input[name=autorizacao]")
                # Número incompleto: o navegador barra com a mensagem do WhatsApp, sem enviar nem travar o botão.
                pagina.fill("#publica-whatsapp", "629888812")
                self.assertEqual(pagina.input_value("#publica-whatsapp"), "(62) 9888-812")
                pagina.locator("[data-enviar]").click()
                pagina.wait_for_timeout(300)
                self.assertTrue(pagina.url.endswith(PAGINA), pagina.url)
                self.assertEqual(pagina.eval_on_selector("#publica-whatsapp", "el => el.validationMessage"),
                                 publica.MENSAGEM_WHATSAPP)
                self.assertTrue(pagina.locator("[data-enviar]").is_enabled())
                self.assertTrue(pagina.locator("[data-enviando]").is_hidden())
                # Colado com o DDI, como o WhatsApp copia: o formulário público usa a mesma máscara do pré-cadastro.
                pagina.fill("#publica-whatsapp", "+55 (62) 98888-1234")
                self.assertEqual(pagina.input_value("#publica-whatsapp"), "(62) 98888-1234")
                with pagina.expect_navigation(wait_until="load"):
                    pagina.locator("[data-enviar]").click()
                self.assertTrue(pagina.url.endswith(RESULTADO), pagina.url)
                self.assertGreater(pagina.locator(".publica-bloqueado").count() + pagina.locator(".publica-item").count(), 0)
                desbloquear = pagina.locator('a[href^="https://wa.me/"]')
                self.assertEqual(desbloquear.count(), 1)
                self.assertTrue(desbloquear.is_visible())
                self._sem_cortes(pagina, f"resultado @ {largura}px")
                pagina.context.close()
