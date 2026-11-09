"""Etapa 4: isolamento de dados entre papéis, empresas e leads.

  1. Matriz papel x rota da área do consultor: TODA rota sob /admin (lida do url_map, então rota nova
     entra sozinha) nega o visitante sem login (redireciona para o login) e o cliente (403), por GET e
     por POST com o token CSRF válido, sem erro 500 e sem dado de lead na resposta.
  2. Cliente de uma empresa não abre a análise nem o PDF da análise PUBLICADA de outra empresa, e abre
     a da própria (controle: sem ele, o 404 viria do status da análise, não da empresa).
  3. Nenhuma rota fora de /admin (todas as do url_map, todos os métodos, com o id real do lead em todo
     parâmetro que não é número e as telas do cliente com conteúdo: análise publicada, chamado e
     notificação dele; o assistente com uma pergunta de verdade) entrega dado de lead ao cliente, quebra
     (5xx ou exceção) ou consulta as tabelas de lead (ouvinte do SQLAlchemy); e, na estrutura, nenhum
     módulo do projeto fora dos da Avaliação de Cenário cita as tabelas ou os módulos de lead.
  4. Leads não se misturam: o diagnóstico (tela e JSON) de um lead só traz as linhas dele, mesmo com
     os mesmos relatórios num segundo lead, com o cache do diagnóstico ativo e com os dois sem linhas.

Dados fictícios.
"""

import json
from pathlib import Path
import re
import tempfile
import uuid

from sqlalchemy import select

import avaliacao_cenario
from tests.deploy_support import DeployTestCase
from tests.raio_x_support import avaliar_como_o_worker
from tests.test_avaliacao_cenario import _relatorio

# Textos que só existem nos leads destes testes.
PRODUTO_DO_LEAD = "Produto Exclusivo do Lead"
CATEGORIA_DO_LEAD = "Categoria Exclusiva do Lead"
CONTATO_DO_LEAD = {"nome_contato": "Contato Exclusivo do Lead", "email": "lead-exclusivo@example.invalid",
                   "whatsapp": "11900000000", "nome_loja": "Loja Exclusiva do Lead"}
TEXTOS_DO_LEAD = (PRODUTO_DO_LEAD, CATEGORIA_DO_LEAD, *CONTATO_DO_LEAD.values())
NEGADO_AO_VISITANTE = (301, 302, 303, 401, 403)


def _relatorio_com_categoria(ano, mes, linhas):
    texto = (f"Relatorio de vendas por produto\nPeriodo: 01/{mes:02d}/{ano} a 28/{mes:02d}/{ano}\n"
             "Codigo;Produto;Categoria;Qtde;Preco Unitario;Valor Total;Total Custo;Estoque\n"
             + "".join(linha + "\n" for linha in linhas))
    return texto.encode("utf-8")


def _url(regra, valores=None):
    """URL de uma regra do url_map com valores fictícios nos parâmetros."""
    valores = valores or {}

    def trocar(encontro):
        conversor, nome = encontro.group(1), encontro.group(2)
        if nome in valores:
            return str(valores[nome])
        if conversor == "uuid":
            return str(uuid.uuid4())
        if conversor == "int":
            return "1"
        return "x"
    return re.sub(r"<(?:(\w+)(?:\([^)]*\))?:)?(\w+)>", trocar, regra.rule)


class _IsolamentoCase(DeployTestCase):
    def setUp(self):
        super().setUp()
        self.fixtures()
        pasta = tempfile.TemporaryDirectory(prefix="nexo-isolamento-")
        self.addCleanup(pasta.cleanup)
        self.pasta = Path(pasta.name)

    def registrar(self, conteudos, contato):
        caminhos = []
        for conteudo in conteudos:
            caminho = self.pasta / f"arquivo_{uuid.uuid4().hex[:8]}.csv"
            caminho.write_bytes(conteudo)
            caminhos.append(str(caminho))
        lead = avaliacao_cenario.registrar(avaliar_como_o_worker(caminhos), contato=dict(contato),
                                           registrado_por=self.admin_id, meses_prata=24)
        self.db.session.commit()
        return lead

    def lead_exclusivo(self):
        meses = [_relatorio_com_categoria(2025, mes, [f"1;{PRODUTO_DO_LEAD};{CATEGORIA_DO_LEAD};10;20,00;200,00;120,00;50",
                                                      "2;Arroz 5kg;Mercearia;5;10,00;50,00;30,00;8"])
                 for mes in (9, 10)]
        return self.registrar(meses, CONTATO_DO_LEAD)

    def regras(self, prefixo):
        return [r for r in self.app.url_map.iter_rules() if r.rule.startswith(prefixo)]

    def assert_sem_dado_de_lead(self, resposta, contexto):
        # PDF e outros binários: o texto que der para ler (o ouvinte de SQL cobre o resto).
        corpo = resposta.get_data().decode("utf-8", errors="replace")
        for texto in TEXTOS_DO_LEAD:
            self.assertNotIn(texto, corpo, f"{contexto}: dado de lead na resposta")


# =============================================================================
# 1. Matriz papel x rota da área do consultor
# =============================================================================
class AreaDoConsultorTests(_IsolamentoCase):
    def _valores(self, lead):
        return {"lead_id": lead.id, "id_analise": self.analise_id, "id_empresa": 1}

    def test_visitante_sem_login_e_negado_em_toda_rota_admin(self):
        lead = self.lead_exclusivo()
        regras = self.regras("/admin")
        self.assertGreaterEqual(len(regras), 50)
        # Com o token CSRF da sessão do visitante, o POST chega à checagem de login (sem ele, para antes, no 400).
        token = self.csrf_token("/auth/login")
        for regra in regras:
            caminho = _url(regra, self._valores(lead))
            for metodo in sorted(regra.methods - {"HEAD", "OPTIONS"}):
                with self.subTest(metodo=metodo, rota=regra.rule):
                    dados = {"csrf_token": token} if metodo == "POST" else None
                    resposta = self.client.open(caminho, method=metodo, data=dados)
                    self.assertIn(resposta.status_code, NEGADO_AO_VISITANTE)
                    if resposta.status_code in (301, 302, 303):
                        self.assertIn("/auth/login", resposta.headers.get("Location", ""))
                    self.assert_sem_dado_de_lead(resposta, f"{metodo} {regra.rule}")

    def test_cliente_recebe_403_em_toda_rota_admin_mesmo_com_csrf(self):
        lead = self.lead_exclusivo()
        self.login()
        token = self.csrf_token("/cliente/dashboard")
        for regra in self.regras("/admin"):
            caminho = _url(regra, self._valores(lead))
            for metodo in sorted(regra.methods - {"HEAD", "OPTIONS"}):
                with self.subTest(metodo=metodo, rota=regra.rule):
                    dados = {"csrf_token": token} if metodo == "POST" else None
                    resposta = self.client.open(caminho, method=metodo, data=dados)
                    self.assertEqual(resposta.status_code, 403)
                    self.assert_sem_dado_de_lead(resposta, f"{metodo} {regra.rule}")


# =============================================================================
# 2. Cliente de uma empresa não vê a análise de outra
# =============================================================================
class EntreEmpresasTests(_IsolamentoCase):
    def _publicar(self, id_analise, texto):
        """A análise concluída e com relatório publicado (como tests/contratos_support.publicar)."""
        from datetime import datetime
        from decimal import Decimal
        from models import Analise, IndicadorAnalise, RelatorioAnalise
        analise = self.db.session.get(Analise, id_analise)
        analise.status_analise = "CONCLUIDO"
        analise.data_conclusao = datetime(2026, 2, 5, 12)
        analise.indicador = IndicadorAnalise(faturamento_total=Decimal("18500"), total_comprado=Decimal("14200"),
                                             saldo_estimado_compras_vendas=Decimal("-4300"))
        analise.relatorio = RelatorioAnalise(titulo="Análise fictícia", conclusao_estrategica=texto, resumo_executivo=texto,
                                             publicado=True, data_publicacao=datetime(2026, 2, 5, 12))
        self.db.session.commit()

    def test_analise_e_pdf_de_outra_empresa(self):
        from models import Empresa
        outra = self.db.session.scalar(select(Empresa).where(Empresa.razao_social == "Empresa fictícia 2"))
        # As duas publicadas: o 404 da outra só pode vir da checagem da empresa.
        self._publicar(self.analise_id, "Conclusão fictícia da própria empresa")
        self._publicar(self.outra_analise_id, "Conclusão fictícia exclusiva da outra empresa")
        self.login()
        propria = self.client.get(f"/cliente/analise/{self.analise_id}")
        self.assertEqual(propria.status_code, 200)
        self.assertIn("Conclusão fictícia da própria empresa", propria.get_data(as_text=True))
        for caminho in (f"/cliente/analise/{self.outra_analise_id}", f"/cliente/analise/{self.outra_analise_id}/pdf"):
            with self.subTest(caminho=caminho):
                resposta = self.client.get(caminho)
                self.assertEqual(resposta.status_code, 404)
                corpo = resposta.get_data(as_text=True)
                for texto in (outra.razao_social, outra.cnpj, "Conclusão fictícia exclusiva da outra empresa"):
                    self.assertNotIn(texto, corpo)


# =============================================================================
# 3. O cliente não alcança dado de lead
# =============================================================================
# Fora da varredura: a saída (por qualquer método) troca a sessão do teste no meio; o pedido de exclusão,
# por POST, dispara o fluxo de exclusão da conta. O assistente entra com JSON (abaixo), não com formulário.
ROTAS_FORA_DA_VARREDURA = {"/auth/logout"}
POST_FORA_DA_VARREDURA = {"/cliente/configuracoes/solicitar-exclusao", "/api/suporte-bot"}
# Módulo do caminho do lead que não é rota nem monta tela: o processo isolado, que despacha a operação da
# Avaliação de Cenário. (O serviço de envio, raio_x_servico, monta o Raio-X do cliente e fica no teste.)
MODULOS_DO_LEAD_EXTRAS = {"etl_worker.py"}
# Pastas fora do teste estrutural: os próprios testes, as migrações e o ambiente.
PASTAS_FORA_DA_ESTRUTURA = {"tests", "migrations", "venv", ".venv", "env", "__pycache__", "node_modules", ".git",
                            "instance", "site-packages"}
# Módulos que podem ler as tabelas de lead: os da Avaliação de Cenário e os modelos. Rota de cliente nova
# que importe um deles (ou cite as tabelas) faz o teste estrutural falhar.
MODULOS_DO_LEAD = {"avaliacao_cenario.py", "avaliacao_motor.py", "blueprints/leads.py", "diagnostico_cenario.py",
                   "etl_runtime.py", "inteligencia_estoque.py", "leads_avaliacao.py", "models.py", "triagem_cenario.py"}
CITACOES_DO_LEAD = ("LeadAvaliacao", "BrainstormingDadosPdv", "lead_avaliacao", "brainstorming_dados_pdv",
                    "avaliacao_cenario", "diagnostico_cenario", "inteligencia_estoque", "triagem_cenario")


class ClienteSemDadoDeLeadTests(_IsolamentoCase):
    def _conteudo_do_cliente(self):
        """A análise do cliente publicada, um chamado e uma notificação dele e o Raio-X da empresa dele, com
        um plano que libera a auditoria: as telas com conteúdo respondem 200 na varredura (um vazamento que
        só aparece com conteúdo não passa pelo 404 ou pelo 403)."""
        import raio_x
        import raio_x_servico
        from models import Analise, ChamadoSuporte, Empresa, Notificacao, Plano
        from tests.raio_x_support import executar_como_o_worker, relatorio_do_mes
        EntreEmpresasTests._publicar(self, self.analise_id, "Conclusão fictícia da própria empresa")
        analise = self.db.session.get(Analise, self.analise_id)
        prata = Plano(nome_plano="PRATA", valor_mensal=300, qtd_analises_mes=1, tipo_analise_permitida="MENSAL",
                      nivel_entrega_analise="COMPLETA", nivel_dashboard="GERENCIAL", nivel_atendimento="MEDIO")
        self.db.session.add(prata)
        self.db.session.flush()
        self.db.session.get(Empresa, analise.id_empresa).id_plano_atual = prata.id_plano
        self.db.session.commit()
        caminhos = []
        for mes in (10, 11):
            caminho = self.pasta / f"raio_x_{mes}.csv"
            caminho.write_bytes(relatorio_do_mes(2025, mes, ["1;Cafe do Cliente 500g;10;20,00;200,00;250,00"]))
            caminhos.append(str(caminho))
        resultado = executar_como_o_worker(caminhos)
        raio_x_servico.salvar(analise.id_empresa, self.cliente_id, resultado, raio_x.recomendar(resultado, 24),
                              [f"vendas-{mes}-2025.csv" for mes in (10, 11)])
        self.db.session.commit()
        chamado = ChamadoSuporte(id_empresa=analise.id_empresa, id_usuario_cliente=self.cliente_id,
                                 assunto="Dúvida fictícia", categoria="DUVIDA_TECNICA")
        notificacao = Notificacao(id_usuario=self.cliente_id, texto="Aviso fictício", link_destino="/cliente/dashboard")
        self.db.session.add_all([chamado, notificacao])
        self.db.session.commit()
        return {"id_analise": self.analise_id, "id_chamado": chamado.id_chamado, "id_notificacao": notificacao.id_notificacao}

    def test_nenhuma_rota_fora_de_admin_entrega_dado_de_lead(self):
        from sqlalchemy import event
        # O id lido antes do ouvinte: ler lead.id de um objeto expirado recarrega o lead (SQL fora de pedido).
        lead_id = self.lead_exclusivo().id
        valores = self._conteudo_do_cliente()
        self.login()
        token = self.csrf_token("/cliente/dashboard")
        rotas = [r for r in self.app.url_map.iter_rules()
                 if not r.rule.startswith(("/admin", "/static"))]
        self.assertGreaterEqual(len(rotas), 30)
        # Guarda em tempo de execução: nenhum comando SQL dos pedidos do cliente cita as tabelas de lead.
        consultas_de_lead = []

        def ouvir(_conexao, _cursor, comando, _parametros, _contexto, _varios):
            if "lead_avaliacao" in comando or "brainstorming_dados_pdv" in comando:
                from flask import has_request_context, request
                origem = f"{request.method} {request.path}" if has_request_context() else "fora de pedido"
                consultas_de_lead.append(f"{origem}: {comando[:100]}")

        event.listen(self.db.engine, "before_cursor_execute", ouvir)
        self.addCleanup(event.remove, self.db.engine, "before_cursor_execute", ouvir)
        chamadas, quebras = 0, []
        for regra in rotas:
            # O id REAL do lead em todo parâmetro que não é número (uuid, texto ou caminho): uma rota por lead
            # responderia 200 a ele. Os números são os do conteúdo do próprio cliente.
            do_lead = {nome: lead_id for nome, conversor in regra._converters.items()
                       if type(conversor).__name__ not in ("IntegerConverter", "FloatConverter")}
            caminho = _url(regra, {**valores, **do_lead})
            for metodo in sorted(regra.methods - {"HEAD", "OPTIONS"}):
                if regra.rule in ROTAS_FORA_DA_VARREDURA or (metodo != "GET" and regra.rule in POST_FORA_DA_VARREDURA):
                    continue
                with self.subTest(metodo=metodo, rota=regra.rule):
                    dados = {"csrf_token": token} if metodo != "GET" else None
                    try:
                        resposta = self.client.open(caminho, method=metodo, data=dados)
                    except Exception as erro:  # noqa: BLE001 - quebra é registrada e reprovada abaixo
                        quebras.append(f"{metodo} {regra.rule}: {type(erro).__name__}")
                        continue
                    chamadas += 1
                    if resposta.status_code >= 500:
                        quebras.append(f"{metodo} {regra.rule}: {resposta.status_code}")
                    self.assert_sem_dado_de_lead(resposta, f"{metodo} {regra.rule}")
        # O assistente do cliente, com uma pergunta de verdade (JSON, como o portal manda).
        resposta = self.client.post("/api/suporte-bot", json={"mensagem": "Quais lojas e produtos foram avaliados?"},
                                    headers={"X-CSRFToken": token})
        self.assertLess(resposta.status_code, 500)
        self.assert_sem_dado_de_lead(resposta, "POST /api/suporte-bot")
        self.assertEqual(quebras, [])
        self.assertEqual(consultas_de_lead, [])
        self.assertGreaterEqual(chamadas, 30)
        # O Raio-X do cliente com conteúdo (a API e a auditoria): a varredura passou por ele com dado.
        self.assertEqual(self.client.get("/api/onboarding/diagnostico").status_code, 200)
        self.assertEqual(self.client.get("/api/onboarding/diagnostico/auditoria").status_code, 200)

    def test_so_o_blueprint_dos_leads_le_as_tabelas_de_lead(self):
        from tests.deploy_support import ROOT
        # Toda rota do blueprint dos leads fica sob /admin (a matriz do teste 1 cobre as permissões).
        for regra in self.app.url_map.iter_rules():
            if regra.endpoint.startswith("leads."):
                self.assertTrue(regra.rule.startswith("/admin/"), regra.rule)
            if "lead" in regra.rule.lower():
                self.assertTrue(regra.rule.startswith("/admin/"), regra.rule)
        # Nenhum outro módulo do projeto (rotas, auxiliares, assistente, tempo real) cita as tabelas ou os
        # módulos de lead.
        candidatos = [caminho for caminho in sorted(ROOT.rglob("*.py"))
                      if not PASTAS_FORA_DA_ESTRUTURA & set(caminho.relative_to(ROOT).parts[:-1])]
        self.assertGreaterEqual(len(candidatos), 40)
        for caminho in candidatos:
            relativo = caminho.relative_to(ROOT).as_posix()
            if relativo in MODULOS_DO_LEAD | MODULOS_DO_LEAD_EXTRAS:
                continue
            fonte = caminho.read_text(encoding="utf-8")
            for citacao in CITACOES_DO_LEAD:
                with self.subTest(modulo=relativo, citacao=citacao):
                    self.assertNotIn(citacao, fonte)


# =============================================================================
# 4. Leads não se misturam
# =============================================================================
class EntreLeadsTests(_IsolamentoCase):
    def test_diagnostico_de_cada_lead_so_com_as_linhas_dele(self):
        mesmos = [_relatorio(2025, mes, ["1;Cafe 500g;10;20,00;200,00;120,00;50", "2;Arroz 5kg;5;10,00;50,00;30,00;8"])
                  for mes in (9, 10)]
        primeiro = self.registrar(mesmos, {**CONTATO_DO_LEAD, "email": "primeiro@example.invalid"})
        # O segundo lead tem os mesmos relatórios e mais um produto: o cache e a chave do produto não podem
        # levar nada de um para o outro.
        segundo = self.registrar(mesmos + [_relatorio(2025, 11, ["3;Feijao Exclusivo do Segundo;6;8,00;48,00;30,00;5"])],
                                 {**CONTATO_DO_LEAD, "email": "segundo@example.invalid", "nome_loja": "Loja do Segundo"})
        self.login("admin-test@example.invalid")
        payloads = {}
        for lead in (primeiro, segundo, primeiro, segundo):
            resposta = self.client.get(f"/admin/leads/{lead.id}/diagnostico.json")
            self.assertEqual(resposta.status_code, 200)
            payload = resposta.get_json()
            self.assertEqual(payload["lead"]["id"], str(lead.id))
            payloads.setdefault(lead.id, payload)
            self.assertEqual(payloads[lead.id], payload)
        texto_primeiro = json.dumps(payloads[primeiro.id], ensure_ascii=False)
        self.assertNotIn("Feijao Exclusivo do Segundo", texto_primeiro)
        self.assertIn("Feijao Exclusivo do Segundo", json.dumps(payloads[segundo.id], ensure_ascii=False))
        self.assertNotEqual(payloads[primeiro.id]["totais"], payloads[segundo.id]["totais"])
        html = self.client.get(f"/admin/leads/{primeiro.id}").get_data(as_text=True)
        self.assertNotIn("Loja do Segundo", html)
        self.assertNotIn("Feijao Exclusivo do Segundo", html)

    def test_leads_sem_linhas_nao_trocam_o_diagnostico(self):
        from sqlalchemy import text
        # Todo lead fica sem linhas depois dos 30 dias: aí só a identidade do lead separa as chaves do cache.
        mesmos = [_relatorio(2025, mes, ["1;Cafe 500g;10;20,00;200,00;120,00;50", "2;Arroz 5kg;5;10,00;50,00;30,00;8"])
                  for mes in (9, 10)]
        primeiro = self.registrar(mesmos, {**CONTATO_DO_LEAD, "email": "primeiro@example.invalid"})
        segundo = self.registrar(mesmos, {**CONTATO_DO_LEAD, "email": "segundo@example.invalid"})
        # Um terceiro, com outros relatórios: o resumo dele não pode aparecer no de outro lead.
        terceiro = self.registrar([_relatorio(2025, 9, ["9;Feijao 1kg;6;8,00;48,00;40,00;5"])],
                                  {**CONTATO_DO_LEAD, "email": "terceiro@example.invalid"})
        self.db.session.execute(text("DELETE FROM brainstorming_dados_pdv"))
        self.db.session.commit()
        self.login("admin-test@example.invalid")
        from unittest.mock import patch
        payloads = {}
        with patch("blueprints.leads.expurgo_oportunista"):
            for lead in (primeiro, segundo, terceiro, primeiro, segundo, terceiro):
                payload = self.client.get(f"/admin/leads/{lead.id}/diagnostico.json").get_json()
                self.assertEqual((payload["lead"]["id"], payload["situacao"]), (str(lead.id), "expirado"))
                self.assertEqual(payloads.setdefault(lead.id, payload), payload)
        # Os campos do próprio lead (relatórios, período, potencial), nunca os de outro.
        for lead in (primeiro, segundo, terceiro):
            gravado = self.db.session.get(type(lead), lead.id)
            dados = payloads[lead.id]["lead"]
            self.assertEqual((dados["arquivos"], dados["periodo"]["inicio"], dados["potencial_recuperavel_total"]),
                             (gravado.arquivos, gravado.periodo_inicio.isoformat(), float(gravado.vazamento_total_reais)))
        self.assertNotEqual(payloads[primeiro.id]["lead"]["arquivos"], payloads[terceiro.id]["lead"]["arquivos"])

    def test_payload_recusa_linha_de_outro_lead(self):
        import diagnostico_cenario
        from blueprints.leads import _lead_para_payload
        from models import LeadAvaliacao
        primeiro = self.lead_exclusivo()
        segundo = self.registrar([_relatorio(2025, 9, ["9;Outro Produto;1;10,00;10,00;5,00;1"])],
                                 {**CONTATO_DO_LEAD, "email": "outro@example.invalid"})
        linhas = avaliacao_cenario.linhas_do_lead(primeiro.id)[0] + avaliacao_cenario.linhas_do_lead(segundo.id)[0]
        from horario import agora_utc
        with self.assertRaises(ValueError):
            diagnostico_cenario.montar_payload(_lead_para_payload(self.db.session.get(LeadAvaliacao, primeiro.id)),
                                               linhas, agora=agora_utc(), retencao_dias=30)
