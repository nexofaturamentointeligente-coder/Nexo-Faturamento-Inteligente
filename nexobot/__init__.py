"""NexoBot Advanced 2.0 — manual interativo e assistente de navegação do portal NEXO.

Camadas (ver Docs/NEXOBOT_ADVANCED.md):
  A. normalizacao  — texto defensivo, abreviações, erros de digitação, citações
  B. classificacao — intenções por evidências e conflitos explícitos (inclui
                     perguntas amplas sobre o produto, aprofundamento e comparações)
  C. conhecimento  — busca BM25F no Guia (GuiaTopico) e na base interna `saber`
                     (174 registros verificados no código, em 4 níveis), com
                     prioridade do Guia e higiene de conteúdo de teste
  D. contexto      — memória mínima da conversa (página, opções, assunto e nível),
                     validada a cada mensagem
  E. servico       — decisão, nível da resposta, blocos estruturados, sugestões e a
                     única ação autorizada (catálogo fechado)
Apoio: saber (base de conhecimento), navegacao (catálogo), seguranca, higiene,
respostas, providers (IA opcional, só redação).

Conhecer não é poder: o NexoBot explica inclusive o trabalho da equipe
administrativa, mas nunca executa operações nem abre páginas restritas.
"""

from .servico import responder

__all__ = ["responder"]
