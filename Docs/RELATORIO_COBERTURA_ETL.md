# Relatório de Cobertura & Resiliência do Motor de ETL — NEXO

> **Objetivo deste documento.** Registrar formalmente, como evidência de teste, a estratégia de resiliência do motor de ETL do NEXO (`etl_processor.py`): a taxonomia de falhas que ele antecipa, a matriz de cenários validados (*chaos engineering*) e o contrato de robustez que o torna um software de nível de produção. É o artefato que sustenta a afirmação: *o motor nunca derruba o sistema e nunca fabrica um KPI falso.*

---

## 1. Contrato de Robustez (a tese central)

Cobertura infinita de formatos é **tecnicamente impossível** — o espaço de entradas é aberto e adversarial (sempre existe um PDF escaneado, uma foto ou um JSON que um parser tabular não reconhece). O que define maturidade de produção não é "ler tudo", e sim um **contrato explícito**:

> **Para qualquer arquivo recebido, o motor garante um de dois desfechos — nunca um terceiro:**
> 1. **Sucesso honesto:** extrai produto + valor e consolida os KPIs reais; ou
> 2. **Recusa acionável:** devolve um diagnóstico claro de qual coluna essencial faltou.
>
> **Jamais ocorre:** queda do servidor (500 não tratado) ou — pior — uma análise "bem-sucedida" calculada sobre dados inventados.

Esse contrato é garantido por uma **rede de segurança final** em `processar_arquivos_analise`, que captura qualquer exceção inesperada e a converte em `sucesso=False` com mensagem segura, isolando a falha da requisição web.

---

## 2. Taxonomia de Falhas (6 camadas) e Defesas Implementadas

| Camada | Vetor de falha | Defesa no motor |
|---|---|---|
| **1. Estrutura** | Linhas em branco/decorativas no topo | `_trim_vazios` (descarta linhas 100% vazias) |
| | Colunas vazias à esquerda / deslocadas | `_trim_vazios` (descarta colunas 100% vazias) |
| | Linhas irregulares em CSV (nº de colunas variável) | Leitura via módulo `csv` + *padding* manual do DataFrame |
| | Cabeçalho em linha distante | Janela de varredura de 100 linhas |
| | Cabeçalho composto (título em 2 linhas) | Concatenação de linhas adjacentes |
| | Múltiplas abas (capa/resumo antes dos dados) | Lê todas as abas e escolhe a com mais células preenchidas |
| **2. Encoding/Separador** | UTF-8 (com/sem BOM), CP1252, Latin-1 | Cascata de *encodings* + fallback `errors="ignore"` |
| | Separadores `;` `,` `tab` `\|` | Escolhe o separador que maximiza o nº de colunas |
| **3. Numérico** | `R$ 1.234,56`, `1,234.56`, `(123)`, `%`, espaços | `_parse_valor` (heurística pt-BR/en-US robusta) |
| **4. Semântica** | `id_produto` confundido com nome do produto | Canônico `codigo` tem prioridade sobre `produto` (ordem do dicionário) |
| | Preço unitário confundido com total | Total explícito sempre vence; unitário só deriva o total na ausência dele |
| | Sinônimos de coluna ("faturamento", "total_custo"…) | Dicionário de *aliases* por substring normalizada |
| **5. Conteúdo** | Rodapés, totais, marca d'água | `_PADRAO_LIXO` (regex) filtra linhas-lixo |
| | Cabeçalho reimpresso (quebra de página do PDV) | `_CABECALHOS_REPETIDOS` |
| | Linhas não-produto (desconto, frete, troco) | `_NAO_PRODUTOS` |
| **6. Processo** | Qualquer exceção inesperada | Rede de segurança: `try/except` em `processar_arquivos_analise` |

---

## 3. Capacidades Semânticas Avançadas

O motor não apenas lê — ele **interpreta** estruturas heterogêneas:

- **Mapeamento dinâmico por aliases:** independe da posição e do nome exato das colunas; casa fragmentos normalizados (sem acento, minúsculas, sem pontuação).
- **Resolução determinística de conflito `id × nome`:** a coluna `id_produto` (ex.: `P001`) é reivindicada como *código*, liberando o canônico `produto` para o nome real (`descricao_produto`/`nome_produto`).
- **Derivação honesta de total:** quando o relatório traz apenas `quantidade` + `valor unitário` (sem coluna de total), o motor **calcula** `total = qtd × unitário`. É aritmética sobre dados reais — não fabricação. O total explícito, quando existe, sempre tem precedência.
- **Compras NF-level OU item-level:** aceita tanto relatórios com `fornecedor` quanto relatórios por produto sem fornecedor.
- **Curva ABC (Pareto) vetorizada** com NumPy (`np.select`), de alta performance mesmo em catálogos grandes.

> **Regra de domínio inegociável (reforçada aqui):** o motor detecta colunas como `lucro`/`custo` apenas para mapeamento defensivo e as **descarta** dos KPIs. O NEXO trabalha só com faturamento, volume de compras e o descasamento estimado (Pressão de Estoque) — nunca lucro, margem, CMV ou rentabilidade.

---

## 4. Matriz de Testes — Layouts Reais (homologados)

Seis layouts reais de PDV/ERP, validados ponta a ponta:

| Layout | Coluna Produto | Coluna Valor | Particularidade | Resultado |
|---|---|---|---|---|
| C1_Hemorragia vendas | `descricao_produto` | `faturamento` | total por linha | ✅ |
| C1_Hemorragia compras | (NF-level) | `total_compra` | `R$` pt-BR + fornecedor | ✅ |
| C4_Equilibrio_ABC vendas | `nome_produto` | `faturamento` | colunas extras (categoria, classe) | ✅ |
| C4_Equilibrio_ABC compras | `nome_produto` | `total_custo` | **item-level, sem fornecedor** | ✅ |
| C3_Ruptura vendas | `descricao` | `Valor_Arrecadado_Total_Vendas` | nome de coluna verboso | ✅ |
| C3_Ruptura compras | `descricao` | `total_custo` + fornecedor | ruptura (linhas com total 0 filtradas) | ✅ |
| C2_Refem vendas | `nome_produto` | `faturamento` | — | ✅ |
| C2_Refem compras | `nome_produto` | **só `custo_unitario`** | **total DERIVADO (qtd × unitário)** | ✅ |

---

## 5. Matriz de Testes — Chaos Engineering (adversarial)

Nove cenários adversariais; os 7 válidos são processados, os 2 sem colunas essenciais são recusados com diagnóstico:

| # | Cenário | Esperado | Resultado |
|---|---|---|---|
| 1 | Cabeçalho na linha 5 (lixo decorativo no topo) | processar | ✅ OK |
| 2 | Colunas em ordem embaralhada | processar | ✅ OK |
| 3 | Cabeçalho composto (título em 2 linhas) | processar | ✅ OK |
| 4 | Colunas deslocadas + rodapé "TOTAL GERAL" | processar (rodapé filtrado) | ✅ OK |
| 5 | Vírgula + Latin-1 + `R$` pt-BR | processar | ✅ OK |
| 6 | Armadilha: `preco_venda` **e** `faturamento` juntos | usar o total, não o unitário | ✅ OK (290, não 330) |
| 7 | Cabeçalho na linha 12 (distante) | processar | ✅ OK |
| 8 | Arquivo ilegível (lista de chamada: aluno/nota/falta) | **recusar com diagnóstico** | ✅ Recusado |
| 9 | Vendas sem nenhuma coluna de valor | **recusar com diagnóstico** | ✅ Recusado |
| 10 | Workbook com aba "Resumo" antes da aba de dados | achar a aba certa | ✅ OK |
| 11 | Separador pipe `\|` | processar | ✅ OK |

---

## 6. Decisões de Escopo — o que foi *deliberadamente* não implementado

Maturidade de engenharia inclui saber **onde parar**. Os itens abaixo foram conscientemente deixados de fora — cobertos pela rede de segurança (recusa com diagnóstico), não por mais código:

- **PDF, imagem, JSON, XML:** fora do escopo (relatório *tabular* de PDV). Adicioná-los seria *scope creep*.
- **Sinônimos infinitos de coluna:** o dicionário cobre os padrões reais; perseguir cada nome possível introduz risco de *mis-mapping* (fragmentos genéricos demais casam coluna errada).
- **Encodings exóticos (UTF-16) e formatos numéricos raros ("1.5K"):** incomuns em PDV; alterá-los arriscaria a cascata de leitura já validada.

> **Princípio:** *adicionar apenas o que é gap real, comum e de baixo risco; o resto é responsabilidade da rede de segurança.* Perseguir "zero gaps para qualquer arquivo do universo" é uma falácia — e, na véspera de uma entrega, código especulativo é mais arriscado (regressão) do que a lacuna marginal que ele cobriria.

---

## 7. Conclusão

O motor de ETL do NEXO foi submetido a uma taxonomia de falhas em 6 camadas e a 11+ cenários de teste (reais e adversariais). Ele processa relatórios em estruturas radicalmente diferentes — NF-level, item-level, sem fornecedor, apenas com valor unitário, cabeçalhos deslocados, encodings e separadores variados, múltiplas abas. E, crucialmente, **recusa com transparência** o que não pode honrar.

A promessa não é "leio qualquer arquivo do mundo". A promessa — verificada empiricamente — é mais forte e mais honesta:

> **O motor nunca quebra o sistema e nunca inventa um número. Ou entrega inteligência real, ou explica exatamente o que faltou.**
