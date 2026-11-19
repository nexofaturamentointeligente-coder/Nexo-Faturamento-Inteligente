"""Higiene da Base de Conhecimento: separar conteúdo de produção de conteúdo de teste.

Contexto: em uso real, o NexoBot apresentou categorias como "ZZTEST-guia" e
"ZZTEST-guia <i>XSS</i>" como assuntos do portal. Esses registros não vêm do
código nem dos testes automatizados (que usam SQLite temporário); foram criados
no banco real pelo CMS, por testes manuais. A exclusão é decisão administrativa.

O detector abaixo é deliberadamente CONSERVADOR: só marca o que carrega sinais
inequívocos de teste (prefixos como "ZZTEST", marcação HTML na categoria ou no
título, categorias chamadas literalmente "teste", textos de preenchimento).
Nunca marca um tópico apenas por conter uma palavra comum.

Efeitos de um tópico marcado (ver conhecimento.py):
  - não entra nas categorias/assuntos apresentados ao cliente;
  - tem a confiança da busca reduzida (não some, mas não vence um tópico real);
  - é listado no log, no CMS do administrador e em `tools/auditar_guia.py`
    para revisão e desativação pelo CMS.
"""

import re
import unicodedata

_PREFIXOS_TESTE = ("zztest", "zz_test", "zz-test", "xxtest", "test_", "teste_", "tmp_", "temp_")
_CATEGORIAS_TESTE = frozenset({"teste", "testes", "test", "tests", "testing", "zz", "zzz", "xxx", "tmp",
                               "temp", "temporario", "temporaria", "sandbox", "debug", "lorem", "lixo"})
_PREENCHIMENTO = ("lorem ipsum", "dolor sit amet", "asdf", "qwerty", "foo bar", "blablabla")
_HTML = re.compile(r"<\s*/?\s*[a-z][a-z0-9]*[^<>]*>", re.IGNORECASE)
_SCRIPT = re.compile(r"<\s*script|javascript:|onerror\s*=|onload\s*=", re.IGNORECASE)


def _simples(texto) -> str:
    texto = unicodedata.normalize("NFKD", str(texto or ""))
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return " ".join(texto.casefold().split())


def motivo_teste(categoria, pergunta="", resposta="") -> str | None:
    """Motivo pelo qual o tópico parece ser conteúdo de teste, ou None."""
    cat, perg, resp = _simples(categoria), _simples(pergunta), _simples(resposta)
    for prefixo in _PREFIXOS_TESTE:
        if cat.startswith(prefixo) or perg.startswith(prefixo):
            return f"prefixo de teste “{prefixo}”"
    if cat in _CATEGORIAS_TESTE:
        return f"categoria “{categoria}” reservada a testes"
    if _SCRIPT.search(str(categoria or "")) or _SCRIPT.search(str(pergunta or "")):
        return "marcação de script no título ou na categoria"
    if _HTML.search(str(categoria or "")):
        return "marcação HTML na categoria"
    for marca in _PREENCHIMENTO:
        if marca in cat or marca in perg or (marca in resp and len(resp) < 200):
            return f"texto de preenchimento “{marca}”"
    if "xss" in cat.split() or "xss" in perg.split():
        return "menção a XSS no título ou na categoria"
    return None
