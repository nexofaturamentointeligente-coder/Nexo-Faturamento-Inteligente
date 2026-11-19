"""
Gerenciamento de análises por empresa (etapa de 28/10/2026).

Cobre os 25 cenários pedidos pelo proprietário, sem navegador (cliente de teste
do Flask, SQLite descartável com as migrações reais, dados fictícios):

  Listagem geral (/admin/analises)
    1-3. abre, mostra as análises existentes e as recém-criadas;
    4.   o botão Abrir leva à análise;
    5.   nenhuma linha tem lixeira (nem o formulário/rota antigos);
    6.   o botão Editar leva ao gerenciamento (só administradores).
  Seleção de empresa (/admin/analises/gerenciar)
    7-12. cartões das empresas ativas, busca, seleção, análises só da empresa
          selecionada, volta à seleção; empresa na lixeira ou inexistente → 404.
  Exclusão (/admin/analises/gerenciar/<empresa>/excluir/<analise>)
    13-22. lixeira só na área da empresa; confirmação; cancelar não exclui;
           confirmar exclui só a escolhida; as outras e a empresa permanecem;
           id de outra empresa recusado; GET não exclui; sem CSRF não exclui;
           CLIENTE não executa; anônimo é redirecionado ao login.
  Integridade
    23. uploads, indicador, relatório e Curva ABC seguem a cascata; o arquivo em
        disco é removido depois do commit;
    24. falha no banco deixa tudo como estava (registros e arquivo);
    25. não há exclusão em massa: uma análise por requisição, sempre com id.
"""

import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from sqlalchemy.exc import OperationalError

from tests.contratos_support import ContratosCase


class GerenciamentoAnalisesTests(ContratosCase):
    def setUp(self):
        super().setUp()
        self.client = self.app.test_client()

    # ------------------------------------------------------------ apoio
    def _admin(self):
        self.login("admin-test@example.invalid")

    def _pagina(self, caminho, status=200):
        resposta = self.client.get(caminho)
        self.assertEqual(resposta.status_code, status, f"{caminho}: {resposta.status_code}")
        return resposta.get_data(as_text=True)

    def _url_excluir(self, id_empresa, id_analise):
        return f"/admin/analises/gerenciar/{id_empresa}/excluir/{id_analise}"

    def _existe(self, id_analise):
        from models import Analise
        self.db.session.expire_all()
        return self.db.session.get(Analise, id_analise) is not None

    # ------------------------------------------------------ listagem geral
    def test_listagem_geral_abre_lista_analises_e_abre_cada_uma(self):
        self._admin()
        nova = self.nova_analise(date(2026, 3, 1), date(2026, 3, 31))
        html = self._pagina("/admin/analises")
        for id_analise in (self.analise_id, self.outra_analise_id, nova.id_analise):
            self.assertIn(f'href="/admin/analise/{id_analise}/upload"', html, f"botão Abrir da análise {id_analise}")
        self.assertIn("Abrir", html)
        # O botão Abrir funciona (página da análise responde).
        self._pagina(f"/admin/analise/{nova.id_analise}/upload")

    def test_listagem_geral_nao_tem_lixeira_em_nenhuma_linha(self):
        self._admin()
        # Análises de todos os tipos e status presentes na listagem.
        quinzenal = self.nova_analise(date(2026, 4, 1), date(2026, 4, 15))
        quinzenal.tipo_analise, quinzenal.quinzena_referencia = "QUINZENAL", 1
        for status in ("RELATORIO_RECEBIDO", "EM_ANALISE"):
            self.nova_analise(date(2026, 5, 1), date(2026, 5, 31), status=status)
        self.db.session.commit()
        html = self._pagina("/admin/analises")
        tabela = html[html.index("<table"):html.index("</table>")]
        self.assertNotIn("bi-trash", tabela)
        self.assertNotIn("Excluir", tabela)
        self.assertNotIn("btn-excluir-analise", html)
        self.assertNotIn("confirmDeleteModal", html)
        self.assertNotIn("confirmExcluirAnalise", html)
        self.assertNotIn("/analise/deletar/", html)
        self.assertNotIn("/excluir/", html)
        self.assertEqual(tabela.count('class="btn-ghost btn-abrir"'), 5)

    def test_listagem_geral_tem_botao_editar_para_administradores(self):
        self._admin()
        html = self._pagina("/admin/analises")
        self.assertIn('id="btnGerenciar"', html)
        self.assertIn('href="/admin/analises/gerenciar"', html)
        self.assertIn("Editar", html)

    def test_rota_antiga_de_exclusao_direta_nao_existe_mais(self):
        self._admin()
        token = self.csrf_token("/admin/analises")
        resposta = self.client.post(f"/admin/analise/deletar/{self.analise_id}", data={"csrf_token": token})
        self.assertEqual(resposta.status_code, 404)
        self.assertTrue(self._existe(self.analise_id))

    # ---------------------------------------------------- seleção de empresa
    def test_gerenciar_mostra_empresas_ativas_em_cartoes_com_busca(self):
        from models import Empresa
        self._admin()
        html = self._pagina("/admin/analises/gerenciar")
        self.assertIn("Gerenciar análises", html)
        self.assertIn("Selecione uma empresa para consultar e gerenciar suas análises.", html)
        self.assertIn('id="buscaGerenciar"', html)
        for id_empresa in (self.empresa_id, self.outra_empresa_id):
            empresa = self.db.session.get(Empresa, id_empresa)
            self.assertIn(f'href="/admin/analises/gerenciar/{id_empresa}"', html)
            self.assertIn(empresa.razao_social, html)
        self.assertEqual(html.count('class="empresa-card"'), 2)
        # Nenhuma lixeira de análise na primeira tela (o ícone do menu "Lixeira" é outra coisa).
        self.assertNotIn("btn-excluir-analise", html)
        self.assertNotIn("/excluir/", html)
        self.assertNotIn("Excluir análise", html)

    def test_gerenciar_cartoes_sao_links_em_lista_real_e_buscaveis_por_cnpj_sem_mascara(self):
        """Acessibilidade e busca: cada cartão é um <a> dentro de <li> (sem role que
        apague a semântica de link) e o CNPJ gravado com máscara também sai sem pontuação
        no atributo usado pela busca; a empresa selecionada é um grupo nomeado."""
        from models import Empresa
        self._admin()
        base = self.db.session.get(Empresa, self.empresa_id)
        self.db.session.add(Empresa(
            id_segmento=base.id_segmento, id_plano_atual=base.id_plano_atual,
            cnpj="11.222.333/0001-44", razao_social="Empresa Mascarada Fictícia Ltda",
            nome_fantasia="Mascarada Teste", email_contato="mascarada@example.invalid",
            data_contratacao=date(2026, 1, 1)))
        self.db.session.commit()
        html = self._pagina("/admin/analises/gerenciar")
        self.assertIn('<ul class="empresa-grid" id="gradeEmpresas" role="list"', html)
        self.assertEqual(html.count('<li class="empresa-item">'), 3)
        self.assertEqual(html.count('class="empresa-card"'), 3)
        self.assertNotIn('role="listitem"', html)
        self.assertIn('data-cnpj="11222333000144"', html)
        self.assertIn('data-cnpj="0000000000000', html)
        html_empresa = self._pagina(f"/admin/analises/gerenciar/{self.empresa_id}")
        self.assertIn('id="empresaSelecionada" role="group" aria-label="Empresa selecionada"', html_empresa)

    def test_gerenciar_nao_lista_empresa_da_lixeira(self):
        from datetime import datetime
        from models import Empresa
        self._admin()
        outra = self.db.session.get(Empresa, self.outra_empresa_id)
        outra.deletado_em = datetime(2026, 2, 1, 12)
        self.db.session.commit()
        html = self._pagina("/admin/analises/gerenciar")
        self.assertNotIn(f'href="/admin/analises/gerenciar/{self.outra_empresa_id}"', html)
        self.assertIn(f'href="/admin/analises/gerenciar/{self.empresa_id}"', html)
        self._pagina(f"/admin/analises/gerenciar/{self.outra_empresa_id}", status=404)

    def test_empresa_selecionada_lista_so_as_proprias_analises(self):
        self._admin()
        extra = self.nova_analise(date(2026, 3, 1), date(2026, 3, 31))
        html = self._pagina(f"/admin/analises/gerenciar/{self.empresa_id}")
        self.assertIn(f'data-analise="{self.analise_id}"', html)
        self.assertIn(f'data-analise="{extra.id_analise}"', html)
        self.assertNotIn(f'data-analise="{self.outra_analise_id}"', html)
        self.assertIn("Voltar à seleção de empresas", html)
        self.assertIn('href="/admin/analises/gerenciar"', html)
        self.assertIn('id="empresaSelecionada"', html)
        self._pagina("/admin/analises/gerenciar/999999", status=404)

    # --------------------------------------------------------------- exclusão
    def test_lixeira_so_na_area_da_empresa_com_confirmacao(self):
        self._admin()
        html = self._pagina(f"/admin/analises/gerenciar/{self.empresa_id}")
        self.assertEqual(html.count("btn-excluir-analise"), 1)
        self.assertIn('id="confirmExcluirAnalise"', html)
        self.assertIn("Excluir análise?", html)
        self.assertIn("Você está prestes a excluir a análise selecionada desta empresa.", html)
        self.assertIn("Confirmar exclusão", html)
        self.assertIn("Cancelar", html)
        self.assertIn(self._url_excluir(self.empresa_id, self.analise_id), html)
        # Cancelar: nenhuma requisição é feita; o estado não muda.
        self.assertTrue(self._existe(self.analise_id))

    def test_confirmar_exclui_somente_a_analise_escolhida(self):
        from models import Empresa
        self._admin()
        extra_id = self.nova_analise(date(2026, 3, 1), date(2026, 3, 31)).id_analise
        token = self.csrf_token(f"/admin/analises/gerenciar/{self.empresa_id}")
        resposta = self.client.post(self._url_excluir(self.empresa_id, extra_id),
                                    data={"csrf_token": token, "confirmar": "sim"})
        self.assertEqual(resposta.status_code, 302)
        self.assertTrue(resposta.headers["Location"].endswith(f"/admin/analises/gerenciar/{self.empresa_id}"))
        self.assertFalse(self._existe(extra_id))
        self.assertTrue(self._existe(self.analise_id))
        self.assertTrue(self._existe(self.outra_analise_id))
        self.assertIsNotNone(self.db.session.get(Empresa, self.empresa_id))
        seguinte = self._pagina(f"/admin/analises/gerenciar/{self.empresa_id}")
        self.assertIn("excluída", seguinte)
        self.assertIn("As demais análises da empresa foram preservadas", seguinte)
        self.assertNotIn(f'data-analise="{extra_id}"', seguinte)
        self.assertIn(f'data-analise="{self.analise_id}"', seguinte)

    def test_analise_de_outra_empresa_e_recusada(self):
        self._admin()
        token = self.csrf_token(f"/admin/analises/gerenciar/{self.empresa_id}")
        resposta = self.client.post(self._url_excluir(self.empresa_id, self.outra_analise_id),
                                    data={"csrf_token": token, "confirmar": "sim"})
        self.assertEqual(resposta.status_code, 404)
        self.assertTrue(self._existe(self.outra_analise_id))
        self.assertTrue(self._existe(self.analise_id))
        # Análise inexistente e sem o campo de confirmação também são recusadas.
        self.assertEqual(self.client.post(self._url_excluir(self.empresa_id, 999999),
                                          data={"csrf_token": token, "confirmar": "sim"}).status_code, 404)
        self.assertEqual(self.client.post(self._url_excluir(self.empresa_id, self.analise_id),
                                          data={"csrf_token": token}).status_code, 400)
        self.assertTrue(self._existe(self.analise_id))

    def test_get_nao_exclui(self):
        self._admin()
        resposta = self.client.get(self._url_excluir(self.empresa_id, self.analise_id))
        self.assertEqual(resposta.status_code, 405)
        self.assertTrue(self._existe(self.analise_id))

    def test_sem_csrf_nao_exclui(self):
        self._admin()
        resposta = self.client.post(self._url_excluir(self.empresa_id, self.analise_id), data={"confirmar": "sim"})
        self.assertEqual(resposta.status_code, 400)
        self.assertTrue(self._existe(self.analise_id))
        resposta = self.client.post(self._url_excluir(self.empresa_id, self.analise_id),
                                    data={"confirmar": "sim", "csrf_token": "token-invalido"})
        self.assertEqual(resposta.status_code, 400)
        self.assertTrue(self._existe(self.analise_id))

    def test_cliente_e_anonimo_nao_executam(self):
        # CLIENTE autenticado: 403 nas três rotas.
        self.login("cliente-test@example.invalid")
        token = self.csrf_token("/cliente/dashboard")
        self.assertEqual(self.client.get("/admin/analises/gerenciar").status_code, 403)
        self.assertEqual(self.client.get(f"/admin/analises/gerenciar/{self.empresa_id}").status_code, 403)
        resposta = self.client.post(self._url_excluir(self.empresa_id, self.analise_id),
                                    data={"csrf_token": token, "confirmar": "sim"})
        self.assertEqual(resposta.status_code, 403)
        self.assertTrue(self._existe(self.analise_id))
        # Anônimo: vai para o login.
        anonimo = self.app.test_client()
        resposta = anonimo.post(self._url_excluir(self.empresa_id, self.analise_id), data={"confirmar": "sim"})
        self.assertIn(resposta.status_code, (302, 400, 401))
        self.assertTrue(self._existe(self.analise_id))
        resposta = anonimo.get("/admin/analises/gerenciar")
        self.assertEqual(resposta.status_code, 302)
        self.assertIn("/auth/login", resposta.headers["Location"])

    # ------------------------------------------------------------ integridade
    def _analise_completa(self):
        """Análise publicada (indicador + relatório), com Curva ABC e um upload em disco."""
        from models import ProdutoCurvaABC, UploadRelatorio
        analise = self.nova_analise(date(2026, 3, 1), date(2026, 3, 31))
        self.publicar(analise.id_analise, inicio=date(2026, 3, 1), fim=date(2026, 3, 31))
        pasta = Path(self.app.config["UPLOAD_FOLDER"])
        pasta.mkdir(parents=True, exist_ok=True)
        arquivo = pasta / f"vendas-teste-{analise.id_analise}.csv"
        arquivo.write_text("produto;qtd\nA;1\n", encoding="utf-8")
        s = self.db.session
        s.add(UploadRelatorio(id_analise=analise.id_analise, id_usuario_admin=self.admin_id, tipo_relatorio="VENDAS",
                              nome_arquivo_original="vendas.csv", caminho_arquivo=str(arquivo), extensao_arquivo="CSV",
                              tamanho_arquivo=16, hash_arquivo="0" * 64, status_processamento="PROCESSADO"))
        s.add(ProdutoCurvaABC(id_analise=analise.id_analise, posicao_ranking=1, produto_nome="Produto fictício",
                              quantidade=Decimal("10"), faturamento=Decimal("100"), percentual_individual=Decimal("100"),
                              percentual_acumulado=Decimal("100"), classe_abc="A"))
        s.commit()
        return analise.id_analise, arquivo

    def _dependentes(self, id_analise):
        from sqlalchemy import func, select
        from models import IndicadorAnalise, ProdutoCurvaABC, RelatorioAnalise, UploadRelatorio
        s = self.db.session
        s.expire_all()
        return {
            "uploads": s.scalar(select(func.count()).select_from(UploadRelatorio).where(UploadRelatorio.id_analise == id_analise)),
            "indicador": s.scalar(select(func.count()).select_from(IndicadorAnalise).where(IndicadorAnalise.id_analise == id_analise)),
            "relatorio": s.scalar(select(func.count()).select_from(RelatorioAnalise).where(RelatorioAnalise.id_analise == id_analise)),
            "abc": s.scalar(select(func.count()).select_from(ProdutoCurvaABC).where(ProdutoCurvaABC.id_analise == id_analise)),
        }

    def test_registros_dependentes_e_arquivo_seguem_a_politica_de_exclusao(self):
        self._admin()
        id_analise, arquivo = self._analise_completa()
        self.assertEqual(self._dependentes(id_analise), {"uploads": 1, "indicador": 1, "relatorio": 1, "abc": 1})
        token = self.csrf_token(f"/admin/analises/gerenciar/{self.empresa_id}")
        resposta = self.client.post(self._url_excluir(self.empresa_id, id_analise), data={"csrf_token": token, "confirmar": "sim"})
        self.assertEqual(resposta.status_code, 302)
        self.assertFalse(self._existe(id_analise))
        self.assertEqual(self._dependentes(id_analise), {"uploads": 0, "indicador": 0, "relatorio": 0, "abc": 0})
        self.assertFalse(arquivo.exists(), "o arquivo do upload deve sair do disco depois do commit")
        # As outras análises e as da outra empresa continuam intactas.
        self.assertTrue(self._existe(self.analise_id))
        self.assertTrue(self._existe(self.outra_analise_id))

    def test_falha_no_banco_nao_deixa_estado_inconsistente(self):
        self._admin()
        id_analise, arquivo = self._analise_completa()
        token = self.csrf_token(f"/admin/analises/gerenciar/{self.empresa_id}")
        commit_real = self.db.session.commit

        def commit_que_falha():
            raise OperationalError("COMMIT", None, Exception("disco cheio (simulado)"))

        with patch.object(self.db.session, "commit", side_effect=commit_que_falha):
            resposta = self.client.post(self._url_excluir(self.empresa_id, id_analise), data={"csrf_token": token, "confirmar": "sim"})
        self.assertEqual(resposta.status_code, 302)
        self.db.session.rollback()
        self.assertTrue(self._existe(id_analise), "a análise deve continuar existindo após a falha")
        self.assertEqual(self._dependentes(id_analise), {"uploads": 1, "indicador": 1, "relatorio": 1, "abc": 1})
        self.assertTrue(arquivo.exists(), "o arquivo não pode ser removido antes do commit")
        seguinte = self._pagina(f"/admin/analises/gerenciar/{self.empresa_id}")
        self.assertIn("Não foi possível excluir a análise", seguinte)
        self.assertIn("Nada foi alterado", seguinte)
        commit_real()

    def test_nao_existe_exclusao_em_massa(self):
        self._admin()
        token = self.csrf_token(f"/admin/analises/gerenciar/{self.empresa_id}")
        # Rota sem identificador de análise não existe; selecionar a empresa não exclui nada.
        self.assertEqual(self.client.post(f"/admin/analises/gerenciar/{self.empresa_id}/excluir/",
                                          data={"csrf_token": token, "confirmar": "sim"}).status_code, 404)
        self.assertEqual(self.client.post(f"/admin/analises/gerenciar/{self.empresa_id}/excluir",
                                          data={"csrf_token": token, "confirmar": "sim"}).status_code, 404)
        self.assertEqual(self.client.post(f"/admin/analises/gerenciar/{self.empresa_id}",
                                          data={"csrf_token": token, "confirmar": "sim"}).status_code, 405)
        self.assertTrue(self._existe(self.analise_id))
        self.assertTrue(self._existe(self.outra_analise_id))


if __name__ == "__main__":
    unittest.main()
