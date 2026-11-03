"""Glossário: termos do NEXO → registro que os define.

Usado para duas coisas:
  - responder "o que é X?" diretamente, mesmo quando X é uma palavra que a
    busca lexical trataria como genérica ("faturamento", "concluído");
  - montar comparações ("qual a diferença entre A, B e C?") a partir dos
    termos citados na pergunta.

As chaves estão normalizadas (minúsculas, sem acentos). Termos compostos são
casados antes dos simples (ver `termos_citados`).
"""

import re

GLOSSARIO = {
    # Produto
    "nexo": "nexo.visao_geral.o_que_e",
    "nexo faturamento inteligente": "nexo.visao_geral.o_que_e",
    "faturamento inteligente": "nexo.visao_geral.o_que_e",
    "portal": "nexo.visao_geral.funcionalidades",
    "nexobot": "nexo.tecnologia.nexobot_ia",
    "bot": "nexo.tecnologia.nexobot_ia",
    "atendimento humano": "nexo.suporte.bot_vs_humano",
    "equipe nexo": "nexo.visao_geral.papeis",
    "consultor": "nexo.visao_geral.papeis",
    "cliente": "nexo.acesso.perfis",
    "admin": "nexo.acesso.perfis",
    "administrador": "nexo.admin.o_que_faz",
    "area administrativa": "nexo.admin.area_restrita",
    # Indicadores
    "faturamento": "nexo.indicadores.faturamento_total",
    "faturamento total": "nexo.indicadores.faturamento_total",
    "total comprado": "nexo.indicadores.total_comprado",
    "compras": "nexo.indicadores.total_comprado",
    "volume de compras": "nexo.indicadores.total_comprado",
    "pressao de estoque": "nexo.indicadores.pressao_estoque",
    "indicador de pressao de estoque": "nexo.indicadores.pressao_estoque",
    "saldo estimado": "nexo.indicadores.pressao_estoque",
    "saldo parado": "nexo.indicadores.saldo_parado",
    "saldo estimado parado": "nexo.indicadores.saldo_parado",
    "estoque parado": "nexo.indicadores.saldo_parado",
    "produto mais vendido": "nexo.indicadores.produto_mais_vendido",
    "maior faturamento": "nexo.indicadores.produto_maior_faturamento",
    "lucro": "nexo.indicadores.pressao_nao_e_lucro",
    "margem": "nexo.indicadores.sem_lucro",
    "cmv": "nexo.indicadores.sem_lucro",
    "prejuizo": "nexo.indicadores.pressao_nao_e_lucro",
    "rentabilidade": "nexo.indicadores.sem_lucro",
    "semaforos": "nexo.indicadores.semaforos",
    "semaforo": "nexo.indicadores.semaforos",
    "sintese executiva": "nexo.indicadores.semaforos",
    "5w2h": "nexo.indicadores.semaforos",
    "curva abc": "nexo.indicadores.curva_abc",
    "abc": "nexo.indicadores.curva_abc",
    "pareto": "nexo.indicadores.pareto",
    "grafico de pareto": "nexo.indicadores.pareto",
    "principio de pareto": "nexo.indicadores.pareto",
    "classe a": "nexo.indicadores.classe_a",
    "classe b": "nexo.indicadores.classe_b",
    "classe c": "nexo.indicadores.classe_c",
    "classes a b e c": "nexo.indicadores.classes_abc",
    "percentual acumulado": "nexo.indicadores.faturamento_acumulado",
    "faturamento acumulado": "nexo.indicadores.faturamento_acumulado",
    "indicadores": "nexo.indicadores.quais",
    "kpi": "nexo.indicadores.quais",
    "kpis": "nexo.indicadores.quais",
    # Análises e status
    "analise": "nexo.analises.o_que_e",
    "periodo de referencia": "nexo.analises.periodo_referencia",
    "mensal": "nexo.analises.mensal_quinzenal",
    "quinzenal": "nexo.analises.mensal_quinzenal",
    "divergencia": "nexo.processamento.divergencia_pdv",
    "divergente": "nexo.processamento.divergencia_pdv",
    "aguardando relatorio": "nexo.analises.status_aguardando",
    "relatorio recebido": "nexo.analises.status_recebido",
    "em analise": "nexo.analises.status_em_analise",
    "concluido": "nexo.analises.status_concluido",
    "concluida": "nexo.analises.status_concluido",
    "publicada": "nexo.analises.processada_vs_publicada",
    "processada": "nexo.analises.processada_vs_publicada",
    "aguardando validacao": "nexo.uploads.status",
    "processado": "nexo.uploads.status",
    # Envio e processamento
    "upload": "nexo.uploads.como_enviar",
    "relatorio de vendas": "nexo.uploads.vendas_vs_compras",
    "relatorio de compras": "nexo.uploads.vendas_vs_compras",
    "vendas": "nexo.uploads.vendas_vs_compras",
    "csv": "nexo.uploads.formatos",
    "xlsx": "nexo.uploads.formatos",
    "xls": "nexo.uploads.formatos",
    "etl": "nexo.processamento.o_que_e_etl",
    "processamento": "nexo.processamento.o_que_e_etl",
    "homologacao": "nexo.uploads.quem_valida",
    "fila de homologacao": "nexo.admin.validar_relatorios",
    # Devolutiva, histórico e PDF
    "devolutiva": "nexo.devolutivas.o_que_e",
    "devolutiva estrategica": "nexo.devolutivas.o_que_e",
    "relatorio estrategico": "nexo.devolutivas.o_que_e",
    "resumo executivo": "nexo.devolutivas.resumo_executivo",
    "conclusao estrategica": "nexo.devolutivas.conclusao_estrategica",
    "pontos positivos": "nexo.devolutivas.pontos_e_recomendacoes",
    "pontos de alerta": "nexo.devolutivas.pontos_e_recomendacoes",
    "recomendacoes": "nexo.devolutivas.pontos_e_recomendacoes",
    "historico": "nexo.historico.abrir",
    "pdf": "nexo.historico.pdf_conteudo",
    # Conta, planos, acesso
    "plano": "nexo.planos.quais",
    "planos": "nexo.planos.quais",
    "bronze": "nexo.planos.diferenca",
    "prata": "nexo.planos.diferenca",
    "ouro": "nexo.planos.diferenca",
    "segmento": "nexo.empresas.segmento",
    "conta ativa": "nexo.empresas.status_conta",
    "conta suspensa": "nexo.empresas.conta_suspensa",
    "conta cancelada": "nexo.empresas.status_conta",
    "suspensa": "nexo.empresas.conta_suspensa",
    "cancelada": "nexo.empresas.status_conta",
    "primeiro acesso": "nexo.primeiros_passos.primeiro_login",
    "sessao": "nexo.acesso.sessao_expirada",
    "senha": "nexo.acesso.recuperar_senha",
    "lixeira": "nexo.admin.lixeira",
    # Suporte, guia, notificações
    "chamado": "nexo.suporte.abrir_chamado",
    "ticket": "nexo.suporte.abrir_chamado",
    "suporte": "nexo.suporte.falar_com_equipe",
    "em atendimento": "nexo.suporte.status_chamado",
    "resolvido": "nexo.suporte.status_chamado",
    "guia": "nexo.guia.o_que_e",
    "base de conhecimento": "nexo.guia.o_que_e",
    "notificacao": "nexo.notificacoes.sininho",
    "notificacoes": "nexo.notificacoes.sininho",
    "sininho": "nexo.notificacoes.sininho",
    # Tecnologia
    "python": "nexo.tecnologia.python_flask",
    "flask": "nexo.tecnologia.python_flask",
    "supabase": "nexo.tecnologia.banco_dados",
    "banco de dados": "nexo.tecnologia.banco_dados",
    "postgres": "nexo.tecnologia.banco_dados",
    "postgresql": "nexo.tecnologia.banco_dados",
    "render": "nexo.tecnologia.render",
    "pandas": "nexo.tecnologia.processamento_dados",
    "plotly": "nexo.tecnologia.graficos",
    "ia": "nexo.tecnologia.nexo_usa_ia",
    "inteligencia artificial": "nexo.tecnologia.nexo_usa_ia",
}

# Termos compostos primeiro, para que "pressao de estoque" vença "estoque".
_TERMOS = sorted(GLOSSARIO, key=len, reverse=True)
_PADRAO = re.compile(r"\b(?:" + "|".join(re.escape(t) for t in _TERMOS) + r")\b")

# Palavras que, sozinhas, são ambíguas demais para uma definição direta
# ("o que é cliente?") sem outro sinal; só valem em comparações.
_SO_EM_COMPARACAO = frozenset({"cliente", "admin", "bot", "portal", "analise", "compras", "vendas",
                               "suspensa", "cancelada", "sessao", "senha", "chamado", "ticket",
                               "mensal", "quinzenal", "publicada", "processada", "processado",
                               "ia", "abc", "concluido", "concluida"})


def termos_citados(canonico: str) -> list[tuple[str, str]]:
    """Pares (termo, id) na ordem em que aparecem no texto canônico, sem repetir ids."""
    vistos, achados = set(), []
    for m in _PADRAO.finditer(canonico):
        termo = m.group(0)
        registro_id = GLOSSARIO[termo]
        if registro_id not in vistos:
            vistos.add(registro_id)
            achados.append((termo, registro_id))
    return achados


def termo_definido(canonico: str) -> tuple[str, str] | None:
    """(termo, id) quando a mensagem cita UM só termo do glossário com definição própria."""
    achados = termos_citados(canonico)
    if len(achados) != 1:
        return None
    termo, registro_id = achados[0]
    if termo in _SO_EM_COMPARACAO:
        return None
    return termo, registro_id


def definicao_direta(canonico: str) -> str | None:
    """Id do registro quando a mensagem é uma definição curta de UM termo do glossário."""
    par = termo_definido(canonico)
    return par[1] if par else None
