"""NexoBot Advanced 2.0 — base de conhecimento (saber): integridade, cobertura e precisão.

Executar da raiz: python -m unittest tests.test_nexobot_saber -v

Três grupos:
  1. Integridade dos registros (schema, fontes existentes, regras editoriais).
  2. Matriz de perguntas obrigatórias e variações: a resposta deve vir do registro
     esperado (ou de um equivalente), ser pertinente e não inventar.
  3. Conversa: níveis, aprofundamento, multiassunto, comparações, contexto e
     separação entre conhecer e poder executar.

As perguntas são respondidas pelo motor completo (`responder`) com o app real
(SQLite temporário), sem tópicos do Guia, para medir a base interna sozinha.
"""

import re
import unittest
from pathlib import Path

from nexobot import saber
from nexobot.higiene import motivo_teste
from nexobot.saber.modelo import validar
from tests.deploy_support import DeployTestCase

ROOT = Path(__file__).resolve().parents[1]
TERMOS_FINANCEIROS_PROIBIDOS = ("lucro real", "margem líquida", "cmv real")
# Preço de plano/assinatura (o NEXO não publica valores). "Preço unitário" de uma coluna
# do PDV e a categoria de chamado "Financeiro (cobrança)" são descrições legítimas.
PRECOS = re.compile(r"R\$\s*\d|\b(?:preco|preço|valor|custo)s? (?:d[oa]s? |de )?(?:plano|planos|assinatura|mensalidade)"
                    r"|\bmensalidades?\b|\bquanto custa\b", re.IGNORECASE)


class IntegridadeDaBaseTests(unittest.TestCase):
    def test_registros_validos_e_ids_unicos(self):
        self.assertGreaterEqual(len(saber.REGISTROS), 150)
        self.assertEqual(saber.validar_tudo(), {})
        self.assertEqual(len({r.id for r in saber.REGISTROS}), len(saber.REGISTROS))

    def test_fontes_citadas_existem_no_repositorio(self):
        faltando = sorted({f for r in saber.REGISTROS for f in r.fontes if not (ROOT / f).exists()})
        self.assertEqual(faltando, [])

    def test_referencias_cruzadas_e_glossario(self):
        for r in saber.REGISTROS:
            for alvo in (*r.relacionados, *((r.proximo,) if r.proximo else ())):
                self.assertIn(alvo, saber.POR_ID, f"{r.id} -> {alvo}")
        for termo, alvo in saber.GLOSSARIO.items():
            self.assertIn(alvo, saber.POR_ID, termo)
        for rotulo, alvo in saber.SUGESTOES_INICIAIS:
            self.assertIn(alvo, saber.POR_ID, rotulo)

    def test_destinos_so_do_catalogo(self):
        from nexobot.navegacao import DESTINOS
        for r in saber.REGISTROS:
            self.assertIn(r.destino, (None, *DESTINOS), r.id)

    def test_regras_editoriais(self):
        """Sem URLs/HTML, sem preços, sem .xls aceito, sem lucro/margem como cálculo do NEXO."""
        for r in saber.REGISTROS:
            corpo = " ".join([r.resumo, r.explicacao, r.detalhes, r.exemplo, r.aviso, *r.passos])
            with self.subTest(id=r.id):
                self.assertEqual(validar(r), [])
                self.assertFalse(PRECOS.search(corpo) and "não" not in corpo.lower() and "fora do portal" not in corpo,
                                 "registro com preço/cobrança sem ressalva")
                self.assertNotRegex(corpo, r"\.xls\b(?! \(Excel antigo\)| não| ou)",
                                    ".xls só pode aparecer como formato NÃO aceito")
                for termo in TERMOS_FINANCEIROS_PROIBIDOS:
                    self.assertNotIn(termo, corpo.lower())

    def test_cobertura_por_assunto(self):
        categorias = {r.categoria for r in saber.REGISTROS}
        for esperada in ("Visão Geral", "Primeiros passos", "Acesso e senha", "Empresa e cadastro", "Planos",
                         "Análises", "Envio de relatórios", "Processamento", "Indicadores", "Curva ABC",
                         "Visão Geral do painel", "Devolutiva", "Histórico e PDF", "Notificações", "Suporte",
                         "Guia", "Equipe NEXO", "Tecnologia"):
            self.assertIn(esperada, categorias)
        self.assertGreaterEqual(sum(len(r.perguntas) for r in saber.REGISTROS), 700)


class HigieneTests(unittest.TestCase):
    def test_detecta_conteudo_de_teste_inequivoco(self):
        for categoria, pergunta in (("ZZTEST-guia", "Pergunta de teste ZZ"), ("ZZTEST-guia <i>XSS</i>", "Teste"),
                                    ("teste", "Qualquer coisa"), ("Upload", "<script>alert(1)</script>"),
                                    ("Guia", "lorem ipsum dolor sit amet"), ("Senhas <b>x</b>", "Como troco?")):
            with self.subTest(categoria=categoria, pergunta=pergunta):
                self.assertIsNotNone(motivo_teste(categoria, pergunta, "resposta"))

    def test_nao_marca_conteudo_legitimo(self):
        for categoria, pergunta in (("Upload", "Como fazer o upload dos relatórios?"), ("Senhas", "Como altero minha senha?"),
                                    ("PDV", "Como exportar os relatórios do meu sistema PDV?"),
                                    ("Testes de integração", "Como testar a conexão com o PDV?"),
                                    ("Suporte", "Fiz um teste e deu erro, o que faço?")):
            with self.subTest(categoria=categoria):
                self.assertIsNone(motivo_teste(categoria, pergunta, "resposta"))


# (pergunta, ids aceitos, fragmento obrigatório na resposta)
OBRIGATORIAS = [
    # Visão geral
    ("O que é o NEXO?", {"nexo.visao_geral.o_que_e"}, "plataforma"),
    ("Como o NEXO funciona?", {"nexo.visao_geral.funcionamento"}, "etapas"),
    ("Para que serve?", {"nexo.visao_geral.para_que_serve"}, "compras e vendas"),
    ("Como minha empresa utiliza a plataforma?", {"nexo.visao_geral.funcionamento"}, "relatórios"),
    ("O NEXO emite notas fiscais?", {"nexo.visao_geral.o_que_nao_e"}, "Não"),
    ("O NEXO é um ERP?", {"nexo.visao_geral.o_que_nao_e"}, "não é um ERP"),
    ("O NEXO substitui meu PDV?", {"nexo.visao_geral.o_que_nao_e"}, "não substitui"),
    # Primeiro acesso
    ("Como começo a usar?", {"nexo.primeiros_passos.roteiro"}, "senha"),
    ("Quem cadastra minha empresa?", {"nexo.primeiros_passos.como_recebo_acesso", "nexo.empresas.cadastro"}, "equipe NEXO"),
    ("Como faço o primeiro login?", {"nexo.primeiros_passos.primeiro_login"}, "senha"),
    ("Como recupero a senha?", {"nexo.acesso.recuperar_senha"}, "Esqueci minha senha"),
    ("Como troco minha senha?", {"nexo.acesso.recuperar_senha"}, "Configurações"),
    ("O que posso fazer em Configurações?", {"nexo.acesso.configuracoes"}, "exporta os dados"),
    ("Como exporto meus dados?", {"nexo.acesso.configuracoes"}, "Configurações"),
    ("Como peço a exclusão dos meus dados?", {"nexo.acesso.configuracoes"}, "Configurações"),
    ("Onde vejo a política de privacidade?", {"nexo.acesso.configuracoes"}, "Configurações"),
    ("Como mudo o telefone de contato?", {"nexo.acesso.configuracoes"}, "Configurações"),
    ("Por que minha sessão expirou?", {"nexo.acesso.sessao_expirada"}, "navegador"),
    # Compras e vendas
    ("Como envio meus arquivos?", {"nexo.uploads.como_enviar"}, "Enviar Relatórios"),
    ("Quais formatos são aceitos?", {"nexo.uploads.formatos"}, ".xlsx"),
    ("Por que preciso enviar dois relatórios?", {"nexo.uploads.por_que_dois"}, "Curva ABC"),
    ("Posso reenviar uma planilha?", {"nexo.uploads.substituir", "nexo.uploads.quando_reenviar"}, "substitui"),
    ("Quem valida meus arquivos?", {"nexo.uploads.quem_valida"}, "equipe NEXO"),
    ("O que acontece se houver erro?", {"nexo.uploads.erros", "nexo.processamento.erros"}, "Erro"),
    # Análises
    ("Como uma análise é criada?", {"nexo.analises.como_e_criada"}, "equipe NEXO"),
    ("O que significa relatório recebido?", {"nexo.analises.status_recebido"}, "dois arquivos"),
    ("O que significa em análise?", {"nexo.analises.status_em_analise"}, "devolutiva"),
    ("Quando posso ver os resultados?", {"nexo.analises.quando_vejo_resultados"}, "publica"),
    ("O que acontece quando a análise é publicada?", {"nexo.devolutivas.publicacao"}, "sininho"),
    ("Qual é a diferença entre mensal e quinzenal?", {"nexo.analises.mensal_quinzenal"}, "OURO"),
    # Indicadores
    ("O que é faturamento?", {"nexo.indicadores.faturamento_total"}, "soma"),
    ("O que significa total comprado?", {"nexo.indicadores.total_comprado"}, "Compras"),
    ("O que é pressão de estoque?", {"nexo.indicadores.pressao_estoque"}, "total comprado menos faturamento"),
    ("Pressão de estoque é lucro?", {"nexo.indicadores.pressao_nao_e_lucro"}, "Não"),
    ("O que é produto mais vendido?", {"nexo.indicadores.produto_mais_vendido"}, "quantidade"),
    ("Como funciona a Curva ABC?", {"nexo.indicadores.curva_abc"}, "80%"),
    ("Qual a diferença entre as classes A, B e C?", {"nexo.indicadores.classes_abc"}, "95%"),
    ("O que significa o gráfico de Pareto?", {"nexo.indicadores.pareto"}, "acumulado"),
    # Relatórios
    ("Como vejo minhas análises anteriores?", {"nexo.historico.abrir", "nexo.historico.consultar_anterior"}, "Histórico"),
    ("Como exporto o PDF?", {"nexo.historico.pdf_exportar"}, "Exportar PDF"),
    ("O que é uma devolutiva estratégica?", {"nexo.devolutivas.o_que_e"}, "resumo executivo"),
    ("Como interpreto as recomendações?", {"nexo.devolutivas.interpretar"}, "prioridade"),
    ("Quem publica o relatório?", {"nexo.devolutivas.quem_prepara"}, "equipe NEXO"),
    # Suporte
    ("Como abro um chamado?", {"nexo.suporte.abrir_chamado"}, "Novo chamado"),
    ("Como acompanho uma resposta?", {"nexo.suporte.acompanhar"}, "Suporte"),
    ("Como reabro um chamado?", {"nexo.suporte.reabrir"}, "nova mensagem"),
    ("Como falo com a equipe?", {"nexo.suporte.falar_com_equipe"}, "Suporte"),
    # Funcionalidades administrativas (explicação conceitual)
    ("O que faz o administrador?", {"nexo.admin.o_que_faz"}, "equipe NEXO"),
    ("Quem controla os planos?", {"nexo.planos.quem_controla", "nexo.admin.planos"}, "equipe NEXO"),
    ("O que é a Lixeira?", {"nexo.admin.lixeira"}, "duas etapas"),
    ("Como o ADMIN publica uma análise?", {"nexo.admin.publicar", "nexo.devolutivas.quem_prepara"}, "Concluído"),
    ("Como funciona a Base de Conhecimento?", {"nexo.admin.guia", "nexo.guia.o_que_e", "nexo.guia.admin_gerencia"}, "tópicos"),
]

# Variações que NÃO aparecem literalmente nas formulações da base.
VARIACOES = [
    ("Quero saber como o sistema funciona?", {"nexo.visao_geral.funcionamento"}),
    ("Me explica todo o funcionamento.", {"nexo.visao_geral.funcionamento"}),
    ("O que vocês fazem?", {"nexo.visao_geral.o_que_e"}),
    ("Como o NEXO ajuda minha empresa?", {"nexo.visao_geral.para_que_serve", "nexo.visao_geral.funcionamento"}),
    ("Quais são as funcionalidades?", {"nexo.visao_geral.funcionalidades"}),
    ("O que acontece depois que envio meus arquivos?", {"nexo.uploads.depois_do_envio"}),
    ("Qual é a função dessa plataforma?", {"nexo.visao_geral.para_que_serve"}),
    ("pra que serve isso aqui", {"nexo.visao_geral.para_que_serve"}),
    ("sou novo, por onde comeco?", {"nexo.primeiros_passos.roteiro"}),
    ("Nunca enviei um relatório.", {"nexo.uploads.primeiro_envio"}),
    ("Enviei a planilha errada.", {"nexo.uploads.substituir"}),
    ("Não aparece meu relatório.", {"nexo.analises.nao_aparece"}),
    ("Não entendi a pressão de estoque.", {"nexo.indicadores.pressao_estoque"}),
    ("pressao de estoque negativa eh boa?", {"nexo.indicadores.pressao_estoque", "nexo.indicadores.pressao_nao_e_lucro"}),
    ("o nexo calcula lucro?", {"nexo.indicadores.sem_lucro", "nexo.indicadores.pressao_nao_e_lucro"}),
    ("Aceita arquivo xls?", {"nexo.uploads.formatos"}),
    ("qual o tamanho maximo do arquivo", {"nexo.uploads.formatos"}),
    ("quais colunas minha planilha precisa ter", {"nexo.uploads.colunas_necessarias"}),
    ("o que eh etl", {"nexo.processamento.o_que_e_etl"}),
    ("os dados sao processados automaticamente?", {"nexo.tecnologia.automatico", "nexo.processamento.quem_controla"}),
    ("o nexobot eh uma ia generativa?", {"nexo.tecnologia.nexobot_ia"}),
    ("o nexo usa inteligencia artificial?", {"nexo.tecnologia.nexo_usa_ia", "nexo.devolutivas.ia"}),
    ("onde ficam armazenados os relatorios?", {"nexo.tecnologia.onde_ficam_arquivos", "nexo.uploads.dados_seguros"}),
    ("o que e o supabase", {"nexo.tecnologia.banco_dados"}),
    ("o que é o render?", {"nexo.tecnologia.render"}),
    ("o sistema usa python?", {"nexo.tecnologia.python_flask"}),
    ("como os graficos sao gerados", {"nexo.tecnologia.graficos", "nexo.dashboard.graficos"}),
    ("quais sao os planos", {"nexo.planos.quais"}),
    ("o que muda com a conta suspensa", {"nexo.empresas.status_conta", "nexo.empresas.conta_suspensa"}),
    ("o que sao os semaforos do painel", {"nexo.indicadores.semaforos"}),
    ("o que é classe A", {"nexo.indicadores.classe_a"}),
    ("o que tem no pdf", {"nexo.historico.pdf_conteudo"}),
    ("para que serve o sininho", {"nexo.notificacoes.sininho"}),
    ("vou receber email quando a analise for publicada?", {"nexo.devolutivas.publicacao", "nexo.notificacoes.email", "nexo.notificacoes.eventos"}),
    ("o que significa concluido", {"nexo.analises.status_concluido"}),
    ("qual a diferenca entre cliente e admin", {"nexo.acesso.perfis"}),
    ("o bot abre chamado pra mim?", {"nexo.suporte.bot_cria_chamado"}),
    ("o que e a devolutiva", {"nexo.devolutivas.o_que_e"}),
    ("a devolutiva e feita por ia?", {"nexo.devolutivas.ia"}),
    ("como a equipe cria uma analise", {"nexo.admin.criar_analises", "nexo.analises.como_e_criada"}),
    ("como interpreto meus indicadores", {"nexo.indicadores.interpretar"}),
    ("o que é o resumo executivo", {"nexo.devolutivas.resumo_executivo"}),
    ("como marco as notificacoes como lidas", {"nexo.notificacoes.nao_lidas"}),
    ("o que significa aguardando validacao", {"nexo.uploads.status"}),
    ("como tiro os relatorios do meu pdv", {"nexo.uploads.exportar_pdv"}),
]


class ConhecimentoBase(DeployTestCase):
    """App real sem tópicos do Guia: mede a base interna sozinha."""

    def setUp(self):
        super().setUp()
        self.fixtures()
        self.app.config["HF_API_TOKEN"] = None
        self.contexto_req = self.app.test_request_context()
        self.contexto_req.push()
        self.addCleanup(self.contexto_req.pop)

    def responder(self, mensagem, contexto=None, pagina_atual=None):
        from nexobot import responder
        return responder(mensagem, contexto=contexto, pagina_atual=pagina_atual)

    def conversa(self, *mensagens):
        contexto, saidas = None, []
        for m in mensagens:
            d = self.responder(m, contexto)
            contexto = d["contexto"]
            saidas.append(d)
        return saidas


class MatrizDePerguntasTests(ConhecimentoBase):
    def test_perguntas_obrigatorias(self):
        falhas = []
        for pergunta, ids, fragmento in OBRIGATORIAS:
            d = self.responder(pergunta)
            assunto = d["meta"].get("assunto")
            if assunto not in ids or fragmento not in d["resposta"]:
                falhas.append((pergunta, assunto, d["meta"]["intencao"], fragmento in d["resposta"]))
        self.assertEqual(falhas, [], "\n".join(map(str, falhas)))

    def test_variacoes_linguisticas(self):
        falhas = []
        for pergunta, ids in VARIACOES:
            d = self.responder(pergunta)
            if d["meta"].get("assunto") not in ids:
                falhas.append((pergunta, d["meta"].get("assunto"), d["meta"]["intencao"]))
        self.assertEqual(falhas, [], "\n".join(map(str, falhas)))

    def test_todo_titulo_encontra_o_proprio_registro(self):
        """Os chips enviam o título do registro: ele precisa voltar ao mesmo assunto (ou a um relacionado)."""
        falhas = []
        for r in saber.REGISTROS:
            d = self.responder(r.titulo)
            aceitos = {r.id, *r.relacionados}
            if d["meta"].get("assunto") not in aceitos:
                falhas.append((r.titulo, d["meta"].get("assunto"), d["meta"]["intencao"]))
        self.assertLessEqual(len(falhas), 3, "\n".join(map(str, falhas)))

    def test_nenhuma_pergunta_sobre_o_produto_cai_no_desconhecido(self):
        for pergunta in ("O que é o NEXO?", "Para que serve esse sistema?", "Como funciona essa plataforma?",
                         "O que vocês fazem?", "Como o NEXO ajuda minha empresa?", "Como começo a usar o sistema?",
                         "Me explica todo o funcionamento.", "Quais são as funcionalidades?",
                         "O que acontece depois que envio meus arquivos?", "Qual é a função dessa plataforma?",
                         "Quero saber como o sistema funciona?"):
            with self.subTest(pergunta=pergunta):
                d = self.responder(pergunta)
                self.assertNotIn(d["meta"]["intencao"], ("desconhecida", "ambiguidade"))
                self.assertNotIn("Não encontrei uma orientação", d["resposta"])
                self.assertIsNotNone(d["meta"].get("assunto"))

    def test_nao_inventa_precos_nem_lucro(self):
        d = self.responder("Qual a diferença entre os planos?")
        self.assertEqual(d["meta"]["assunto"], "nexo.planos.diferenca")
        self.assertNotRegex(d["resposta"], r"R\$\s*\d")
        d = self.responder("Quanto custa o plano Ouro?")
        self.assertNotRegex(d["resposta"], r"R\$\s*\d")
        self.assertIn("equipe NEXO", d["resposta"])
        d = self.responder("Pressão de estoque é lucro?")
        self.assertTrue(d["resposta"].lstrip("*").startswith(("Pressão", "Não")) or "Não." in d["resposta"])
        self.assertIn("não representa lucro", d["resposta"])
        d = self.responder("Posso enviar xls?")
        self.assertEqual(d["meta"]["assunto"], "nexo.uploads.formatos")
        self.assertIn("não é aceito", d["resposta"])


class ConversaTests(ConhecimentoBase):
    def test_niveis_de_resposta(self):
        curto = self.responder("Resumidamente, o que é a Curva ABC?")
        self.assertEqual(curto["meta"]["nivel"], 1)
        self.assertEqual([b["tipo"] for b in curto["blocos"]], ["texto"])
        explicacao = self.responder("O que é a Curva ABC?")
        self.assertEqual(explicacao["meta"]["nivel"], 2)
        self.assertIn("cabecalho", [b["tipo"] for b in explicacao["blocos"]])
        passos = self.responder("Como envio meus relatórios?")
        self.assertEqual(passos["meta"]["nivel"], 3)
        self.assertIn("passos", [b["tipo"] for b in passos["blocos"]])
        profundo = self.responder("Me explica a Curva ABC em detalhes.")
        self.assertEqual(profundo["meta"]["nivel"], 4)
        tipos = [b["tipo"] for b in profundo["blocos"]]
        self.assertIn("secao", tipos)
        self.assertIn("exemplo", tipos)

    def test_aprofundamento_sem_repetir(self):
        a, b, c, d, e = self.conversa("O que significa pressão de estoque?", "Explique melhor", "Me dê um exemplo",
                                      "Pode resumir?", "Não entendi")
        self.assertEqual(a["meta"]["assunto"], "nexo.indicadores.pressao_estoque")
        self.assertEqual(a["meta"]["nivel"], 2)
        self.assertEqual(b["meta"]["intencao"], "aprofundar")
        self.assertEqual(b["meta"]["nivel"], 4)
        self.assertNotIn(a["blocos"][1]["texto"], b["resposta"])  # resumo não é repetido
        self.assertIn("Em detalhe", b["resposta"])
        self.assertIn("hipotético", c["resposta"])
        self.assertEqual([x["tipo"] for x in d["blocos"]], ["texto", "aviso"])
        self.assertIn("Vou explicar de outra forma", e["resposta"])

    def test_e_depois_segue_o_roteiro(self):
        a, b, c = self.conversa("Como o NEXO funciona?", "E depois?", "Qual é o próximo passo?")
        self.assertEqual(a["meta"]["assunto"], "nexo.visao_geral.funcionamento")
        self.assertEqual(b["meta"]["assunto"], "nexo.uploads.como_enviar")
        self.assertIn("Próximo passo", b["resposta"])
        self.assertEqual(c["meta"]["assunto"], "nexo.uploads.depois_do_envio")

    def test_como_funciona_isso_usa_o_assunto_em_foco(self):
        a, b = self.conversa("Como envio os relatórios?", "Como funciona isso?")
        self.assertEqual(b["meta"]["intencao"], "aprofundar")
        self.assertEqual(b["meta"]["assunto"], "nexo.uploads.como_enviar")
        sem_contexto = self.responder("Como funciona?")
        self.assertEqual(sem_contexto["meta"]["assunto"], "nexo.visao_geral.funcionamento")

    def test_onde_fica_depois_de_um_assunto(self):
        a, b = self.conversa("Como exporto o PDF?", "Me mostra onde fica")
        self.assertEqual(b["meta"]["intencao"], "aprofundar")
        self.assertEqual((b["acao"], b["destino"]), ("sugerir_navegacao", "historico"))

    def test_multiassunto(self):
        d = self.responder("Como envio os relatórios e quando posso ver os resultados?")
        self.assertIn("Enviar Relatórios", d["resposta"])
        self.assertIn("publica", d["resposta"])
        self.assertEqual(sum(1 for b in d["blocos"] if b["tipo"] == "cabecalho"), 2)

    def test_comparacao(self):
        d = self.responder("Qual a diferença entre faturamento, compras e pressão de estoque?")
        self.assertEqual(d["meta"]["intencao"], "comparacao")
        secoes = [b for b in d["blocos"] if b["tipo"] == "secao"]
        self.assertEqual(len(secoes), 3)
        self.assertIn("aviso", [b["tipo"] for b in d["blocos"]])
        d = self.responder("Qual a diferença entre classe A e classe C?")
        self.assertEqual(d["meta"]["assunto"], "nexo.indicadores.classes_abc")
        d = self.responder("Qual a diferença entre o NexoBot e o atendimento humano?")
        self.assertEqual(d["meta"]["assunto"], "nexo.suporte.bot_vs_humano")

    def test_sugestoes_e_blocos_no_contrato(self):
        d = self.responder("Oi")
        self.assertEqual([s["rotulo"] for s in d["sugestoes"]][:2], ["Como funciona o NEXO?", "Enviar relatórios"])
        self.assertNotIn("ZZTEST", d["resposta"])
        d = self.responder("ajuda")
        self.assertTrue(d["sugestoes"])
        self.assertLess(len(d["resposta"]), 600)
        d = self.responder("O que é o NEXO?")
        for bloco in d["blocos"]:
            self.assertIn(bloco["tipo"], ("cabecalho", "texto", "passos", "secao", "exemplo", "aviso"))
        for chip in d["sugestoes"]:
            self.assertEqual(set(chip), {"rotulo", "mensagem"})
            self.assertLessEqual(len(chip["rotulo"]), 60)
        self.assertLessEqual(len(d["sugestoes"]), 4)

    def test_dados_da_propria_conta_apontam_para_configuracoes(self):
        for pedido in ("trocar meu telefone", "alterar meu email", "mudar meu nome"):
            with self.subTest(pedido=pedido):
                d = self.responder(pedido)
                self.assertEqual(d["destino"], "configuracoes", d)
                self.assertIn("Configurações", d["resposta"])
        self.assertEqual(self.responder("Altere meu cadastro.")["destino"], "suporte")

    def test_conhecer_nao_e_poder_executar(self):
        explica = self.responder("Como o administrador cria uma análise?")
        self.assertIn(explica["meta"]["assunto"], ("nexo.admin.criar_analises", "nexo.analises.como_e_criada"))
        self.assertIsNone(explica["acao"]) if explica["destino"] is None else None
        for pedido in ("Crie uma análise para mim.", "Publique minha análise.", "Exclua minha empresa.",
                       "Mude meu plano para Ouro.", "Altere meu cadastro."):
            with self.subTest(pedido=pedido):
                d = self.responder(pedido)
                self.assertEqual(d["meta"]["intencao"], "operacao_nao_permitida")
                self.assertNotEqual(d["acao"], "navegar")
        d = self.responder("Me leve para a área administrativa.")
        self.assertEqual(d["meta"]["intencao"], "acesso_restrito")
        self.assertIsNone(d["acao"])

    def test_contexto_de_assunto_e_validado(self):
        d = self.responder("Explique melhor", contexto={"assunto": "nexo.inexistente.x", "nivel": 2})
        self.assertNotEqual(d["meta"]["intencao"], "aprofundar")
        d = self.responder("Explique melhor", contexto={"assunto": "../../etc/passwd", "nivel": 9})
        self.assertNotEqual(d["meta"]["intencao"], "aprofundar")
        d = self.responder("Explique melhor", contexto={"assunto": "nexo.indicadores.curva_abc", "nivel": 2})
        self.assertEqual(d["meta"]["intencao"], "aprofundar")
        self.assertEqual(d["contexto"]["assunto"], "nexo.indicadores.curva_abc")
        self.assertEqual(d["contexto"]["nivel"], 4)

    def test_desconhecido_pede_esclarecimento_sem_inventar(self):
        d = self.responder("xptoqwe asdkjh zzzz")
        self.assertIn(d["meta"]["intencao"], ("desconhecida", "duvida_funcional"))
        self.assertTrue("reformular" in d["resposta"] or "Não encontrei" in d["resposta"]
                        or "Não tenho certeza" in d["resposta"])
        self.assertTrue(d["sugestoes"])


class GuiaComTopicosDeTesteTests(DeployTestCase):
    def setUp(self):
        super().setUp()
        # As migrations de teste chamam fileConfig() do Alembic, que desativa os loggers
        # já criados; reativa o do módulo para observar o aviso de higiene.
        import logging
        logging.getLogger("nexobot.conhecimento").disabled = False

    def test_categorias_de_teste_nao_aparecem_e_sao_registradas(self):
        from models import GuiaTopico
        from nexobot.conhecimento import obter_base
        self.fixtures()
        self.db.session.add_all([
            GuiaTopico(categoria="Upload", pergunta="Como fazer o upload dos relatórios?", resposta="Em Enviar relatórios."),
            GuiaTopico(categoria="ZZTEST-guia", pergunta="Pergunta de teste ZZ", resposta="Resposta de teste."),
            GuiaTopico(categoria="ZZTEST-guia <i>XSS</i>", pergunta="Teste XSS", resposta="<i>XSS</i>"),
        ])
        self.db.session.commit()
        with self.assertLogs("nexobot.conhecimento", level="WARNING") as registro:
            base = obter_base()
        self.assertEqual(base.categorias(), ["Upload"])
        self.assertEqual(len(base.topicos_suspeitos()), 2)
        self.assertIn("sinais de conteúdo de teste", "\n".join(registro.output))
        # O tópico de teste continua existindo (não é escondido silenciosamente), com prioridade reduzida.
        self.assertEqual(len(base.topicos), 3)
        from nexobot import responder
        with self.app.test_request_context():
            for mensagem in ("Oi", "ajuda", "O que você faz?"):
                self.assertNotIn("ZZTEST", responder(mensagem)["resposta"], mensagem)
            # Só uma pergunta praticamente idêntica alcança o tópico, e com confiança reduzida.
            d = responder("Pergunta de teste ZZ")
            self.assertLessEqual(d["meta"]["confianca"], 0.5)
            self.assertNotIn("ZZTEST", d["resposta"])
            self.assertNotIn("ZZTEST", " ".join(s["rotulo"] for s in d["sugestoes"]))

    def test_cms_do_administrador_sinaliza_topicos_de_teste(self):
        from models import GuiaTopico
        self.fixtures()
        self.db.session.add(GuiaTopico(categoria="ZZTEST-guia", pergunta="Pergunta de teste ZZ", resposta="x"))
        self.db.session.commit()
        self.login("admin-test@example.invalid")
        pagina = self.client.get("/admin/guia").get_data(as_text=True)
        self.assertIn("Possível teste", pagina)
        self.assertIn("sinais de conteúdo de teste", pagina)


if __name__ == "__main__":
    unittest.main()
