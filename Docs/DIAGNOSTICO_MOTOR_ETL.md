# Diagnóstico do Motor ETL

Mapeamento técnico do motor atual, feito antes de implementar a Avaliação de Cenário e a Ingestão de Massa Bruta. O objetivo é preservar todas as regras acumuladas e evitar código duplicado ou o descarte acidental de uma regra antiga.

- **Base:** commit `91e5fb0`, de 8 de novembro de 2026. As linhas citadas valem para esse commit.
- **Escopo:** só diagnóstico. Nenhum arquivo de código, migração ou banco foi alterado.
- **Inventário completo:** os 538 itens levantados (componentes, regras, formatos e limites), cada um com arquivo, função e linhas, estão no anexo [DIAGNOSTICO_MOTOR_ETL_INVENTARIO.md](DIAGNOSTICO_MOTOR_ETL_INVENTARIO.md). Este documento resume e cita os mais importantes.

**Como foi feito**

1. **Mapeamento.** Cinco mapeamentos independentes, um por recorte: entrada e Baia, cálculo da análise, Raio-X, tabelas de lead e volume.
2. **Avaliação.** Duas avaliações separadas, uma por abordagem de arquitetura.
3. **Julgamento.** Um juiz comparou as duas avaliações.
4. **Conferência das citações.** As citações de arquivo, símbolo e linha foram conferidas automaticamente contra o código: 632 itens. Os 30 avisos do verificador eram intervalos múltiplos ou símbolos descritivos, não erros.
5. **Medições.** Foram feitas com dados sintéticos de até 3 anos, numa cópia do repositório sem `.env`, idêntica ao `91e5fb0` nos 210 arquivos conferidos.
6. **Limite das medições.** Neste Windows o processo isolado do ETL não roda, porque falta o módulo `resource`. Por isso as funções foram medidas no próprio processo, sem os limites do sistema operacional. Os números de tempo valem para esta máquina. O Render é bem mais lento (seção 2.3).
7. **Revisão.** Quatro verificadores independentes conferiram contra o código as citações e afirmações deste relatório (cerca de 730 conferências), e um crítico conferiu a cobertura dos quatro tópicos do pedido. Os 18 apontamentos e as 7 lacunas encontrados foram corrigidos nesta versão.

## Sumário executivo

1. **O motor é uma cadeia de módulos, não um arquivo.**
   - `etl_inputs.read_raw` lê o arquivo, e `security_gatekeeper.neutralizar_matriz` neutraliza fórmulas.
   - `etl_processor._vendas_do_bruto` faz a limpeza.
   - Depois vem uma de duas camadas de regras: `etl_processor._calcular_kpis_local` (análise) ou `raio_x.diagnosticar` (Raio-X).
   - Tudo isso roda num processo filho isolado (`etl_worker.py`), sem banco, rede nem segredos. **Quem grava no banco é sempre o processo pai.**
2. **A higienização acumulada já é única e compartilhada.**
   - A análise mensal e o Raio-X usam a mesma leitura (`read_raw`, já com a neutralização de fórmulas) e a mesma limpeza (`_vendas_do_bruto`).
   - A Baia usa a mesma leitura e, da limpeza, só a detecção de cabeçalho, com um critério mínimo mais brando.
   - Não existe um segundo limpador. Esse é o ativo a preservar: qualquer fluxo novo deve chamar essas funções, e não reescrevê-las.
3. **`raio_x_servico.py` não é o motor.**
   - Ele orquestra: recebe os arquivos, faz a primeira triagem da Baia, chama o processo isolado, grava o JSON e recorta o resultado por plano.
   - O motor de regras do Raio-X é `raio_x.py`, que herda toda a limpeza do `etl_processor`.
   - Nenhum dos dois toca as tabelas de lead.
4. **Detecção de imperfeições do PDV:**
   - **Venda abaixo do custo:** existe em duas versões que divergem. O alerta da análise compara a coluna de custo com o total da linha sem saber se ela é custo unitário ou total; o Raio-X decide pelo título da coluna e multiplica o custo unitário pela quantidade.
   - **Custo R$ 0,00:** só o Raio-X detecta.
   - **Omissão de relatórios consolidados do ERP:** não existe detecção. O "scanner de omissão" do Raio-X é uma heurística de variação de preço.
   - **Defasagem de margem bruta:** existe só no Raio-X, como "precificação desatualizada" e "margem bruta estimada", liberadas por plano. A análise mensal proíbe margem por regra.
5. **Nada existe ainda para os leads.**
   - Nenhum código insere linhas em `lead_avaliacao` ou `brainstorming_dados_pdv`. O único que mexe nelas é o expurgo de 30 dias.
   - Não existe triagem Urgência/Atenção/Eletiva.
   - As vendas não têm data por linha, e não há agregação por ano.
6. **Volume de 3 anos com os limites atuais:**
   - **Tempo:** 36 relatórios mensais cabem no prazo de 120 s só em CSV e numa máquina rápida. Em XLSX, 36 meses × 5.000 produtos levaram 173 s.
   - **CPU no Render:** o plano gratuito tem 0,1 CPU.
   - **Saída:** a do processo isolado tem teto rígido de 4 MiB, o que obriga a usar formato colunar.
   - **Vaga única:** cada processamento ocupa a única vaga do servidor.
7. **Recomendação de arquitetura: um híbrido com núcleo na Abordagem A.** É uma operação nova do processo isolado que reaproveita o motor do Raio-X numa passada única. Da Abordagem B entram duas ideias:
   - uma staging ampliada, com as somas que as regras usam;
   - a triagem como função pura, que pode ser refeita durante os 30 dias de retenção.

   As formas literais de A e de B não se sustentam neste código (seção 4).
8. **Riscos a corrigir antes de crescer o volume.** Seis defeitos de regra foram medidos e devem ser corrigidos com testes antes de qualquer expansão (seção 5.2):
   - rodapés "Total do dia" contados como produto;
   - um filtro de lixo que apaga produtos reais, como "Filtro de Café";
   - quantidade "1/2" lida como 12;
   - aliases comuns de ERP não reconhecidos;
   - NF cancelada somada nas compras;
   - alerta de custo sem distinção de custo unitário.

---

## 1. Mapeamento e anatomia do motor

### 1.1 Módulos e responsabilidades

| Arquivo | Onde roda | Papel | Funções e constantes principais (linha de início) |
|---|---|---|---|
| `etl_limits.py` (93 linhas) | pai e filho | Política finita de recursos (SEC-005) | `ETLLimits` (9), `_HARD_CAPS` (44), `get_limits`, `using_limits` |
| `etl_inputs.py` (638) | filho | Leitura única de CSV e XLSX até a matriz de texto | `ENCODINGS` (26), `SEPARATORS` (27), `_csv_signature` (134), `_csv_input` (148), `_zip_preflight` (479), `_filled` (520), `_disambiguate_numbers` (530), `_xlsx_input` (548), `_run` (601), `read_raw` (625), `validate_input` (636) |
| `security_gatekeeper.py` (758) | pai (assinatura) e filho (triagem) | Baia de Segurança: teto, assinatura, neutralização de fórmula, inventário do XLSX e cópia só com valores | `teto_bytes` (104), `identificar_conteudo` (190), `inspecionar_arquivo` (217), `ler_liberado` (240), `registrar_recusa` (265), `neutralizar_celula` (327), `neutralizar_matriz` (359), `inventariar_xlsx` (480), `exigir_estrutura_minima` (522), `triar` (733) |
| `etl_processor.py` (1187) | filho (cálculo) e pai (persistência) | Cabeçalho, limpeza, números, situação, período, KPIs, Curva ABC, alertas, trava do PDV e gravação | `_norm_chave` (102), `_normalizar_produto` (120), `_nome_exibivel` (133), `_NAO_PRODUTOS` (146), `_parse_valor` (156), `_limpar_monetario` (222) a `_converter_serie_monetaria` (308), `_ler_bruto` (342), `ALIAS_VENDAS` (359), `ALIAS_COMPRAS` (389), `_detectar_cabecalho` (437), `_PADRAO_LIXO` (493), `_filtrar_linhas_validas` (524), `_situacao_das_linhas` (557), `_vendas_do_bruto` (629), `carregar_compras` (684), `_extrair_periodo_base` (751), `_classificar_curva_abc` (784), `_calcular_kpis_local` (896), `validar_total_pdv` (1072), `_persistir_indicadores` (1108), `_persistir_curva_abc` (1134), `processar_arquivos_analise` (1162) |
| `raio_x.py` (544) | filho (o `recomendar` roda no pai) | Regras do Raio-X do PDV: custo unitário, preço de referência, vazamento, perda, saúde do cadastro, margem estimada, precificação e recomendação de plano | `VERSAO_REGRAS` (62), limiares (65-72), `custo_e_unitario` (85), `_codigos` (132), `_conflitos` (139), `_ler_arquivo` (154), `_precificacao` (338), `_avisos` (375), `diagnosticar` (397), `recomendar` (512) |
| `etl_worker.py` (104) | filho | Processo isolado: limites de memória, CPU e arquivo, *audit hook* sem rede, operações fixas `validate`, `calculate` e `raio_x`, saída JSON limitada | `main` (8), `refuse_external` (44), `encode` (89) |
| `etl_runtime.py` (146) | pai | Sobe o filho, aplica prazo e vaga única de job, e valida o contrato de retorno | `_ADMISSION` (17), `_run_job` (44), `run_validation` (106), `run_raio_x` (117), `run_calculation` (131) |
| `etl_uploads.py` (113) | pai | Vaga única de entrada e cópia limitada do upload, com a Baia | `_UPLOAD_ADMISSION` (19), `limitar_upload` (40), `vaga_de_entrada` (45), `limitar_processamento` (56), `ler_upload_validado` (61) |
| `raio_x_servico.py` (216) | pai | Orquestração e persistência do Raio-X (seção 3) | `_gravar_limitado` (47), `processar_envio` (69), `salvar` (116), `ultimo` (138), `visao` (173) |
| `validacao_pdv.py` (200) | pai | Total digitado do PDV, conferência e bloqueio de publicação | `interpretar_valor_digitado` (70), `registrar_valor_informado` (92), `conferir_e_registrar` (110), `motivo_bloqueio_publicacao` (181) |
| `storage.py` (309) | pai | Relatórios gravados como BYTEA e cópia privada para o cálculo | `salvar`, `materializar_para_etl` (145-220) |
| `insights.py` (114) | pai | Semáforos crítico/atenção/oportunidade da análise | `PRESSAO_CRITICA` (27), `PRESSAO_ATENCAO` (28), `gerar_semaforos` (39) |
| `leads_avaliacao.py` (107) | pai | Expurgo dos dados de PDV dos leads e comando `flask leads` | `RETENCAO_DIAS` (27), `expurgar_dados_pdv` (37), `registrar_comandos` (82) |
| `blueprints/cliente.py`, `blueprints/admin.py`, `blueprints/onboarding.py` | pai | Rotas de upload, processamento, publicação e Raio-X | `cliente.upload`, `admin.analise_upload`, `admin.processar_analise` (1389-1478), `admin.analise_relatorio`, `onboarding.diagnostico_enviar` (142-162), `onboarding._executar_raio_x` (114-132) |
| `models.py` | pai | Tabelas gravadas pelo motor e tabelas de lead | `IndicadorAnalise` (404-429), `ProdutoCurvaABC` (540-577), `DiagnosticoEntrada` (1257-1296), `LeadAvaliacao` e `BrainstormingDadosPdv` (1302-1402) |

Consumidores dos KPIs, que não fazem parte do motor:
- `pdf_export.py`: o PDF;
- `ai_devolutiva.py`: o contexto da devolutiva por IA;
- `contract_policy.periodo_analise` (252-266): a cobertura contratual pelo período lido do cabeçalho;
- `templates/admin/processar_resultado.html`: a única tela com todos os alertas, que somem em 9 s.

### 1.2 Arquitetura de execução

```
Rota (processo web, sem pandas no caminho do arquivo)
  ├─ CSRF global (app.py 134) e corpo de até 20 MiB (config.py 227-229)
  ├─ vaga única de entrada: etl_uploads._UPLOAD_ADMISSION (19)
  ├─ cópia em blocos de 64 KiB até teto+1, com SHA-256 (etl_uploads.ler_upload_validado / raio_x_servico._gravar_limitado)
  └─ Baia, 1ª triagem: assinatura dos primeiros 8 KiB (security_gatekeeper.inspecionar_arquivo 217-237)
        │
        ▼
etl_runtime._run_job (44-103): vaga única de job (_ADMISSION), pedido JSON de até 16 KiB,
subprocesso "python -I -B etl_worker.py", prazo de 60 s (ou 120 s em validate e raio_x)
        │
        ▼
etl_worker.main (filho isolado): RLIMIT_AS 384 MiB, CPU, FSIZE e CORE (24-33);
audit hook sem rede nem subprocesso (35-47); sem banco
  ├─ validate   → security_gatekeeper.triar (read_raw + estrutura mínima + inventário + cópia)
  ├─ calculate  → etl_processor._calcular_kpis_local (vendas + compras)
  └─ raio_x     → validate_input de cada arquivo + raio_x.diagnosticar (1 a 36 arquivos)
        │  JSON com allow_nan=False, teto de 4 MiB (etl_worker.py 72-99)
        ▼
Processo pai: valida chaves e tipos do retorno (etl_runtime.py 126; 135-146) e grava
  ├─ análise: indicador_analise e produto_curva_abc num SAVEPOINT (etl_processor 1162-1187)
  └─ Raio-X:  diagnostico_entrada (raio_x_servico.salvar 116-135)
```

Há duas vagas por processo, e o servidor roda com um processo só (`gunicorn -w 1`, em `render.yaml`). Por isso só um upload, processamento ou Raio-X roda por vez no servidor inteiro, sem fila.

### 1.3 Fluxo 1: upload de um relatório (cliente ou consultor)

1. O Werkzeug lê o multipart com o corpo inteiro limitado a 20 MiB. Acima disso, responde 413 (`config.py` 227-229; `app.py` 470-481).
2. A rota exige login e perfil, 30 POST por hora no cliente e a vaga única. Sem vaga, mostra um aviso e volta sem ler o arquivo (`blueprints/cliente.py` 609-614; `etl_uploads.py` 22-58).
3. A rota confere a análise, o tipo (VENDAS ou COMPRAS), `secure_filename` e a extensão em `ALLOWED_EXTENSIONS = {'csv','xlsx'}` (`blueprints/cliente.py` 636-663; `config.py` 275-277). O consultor tem a mesma validação em `blueprints/admin.py` 986-1006.
4. No envio de VENDAS, o total do PDV digitado é validado antes de ler o arquivo (`validacao_pdv.interpretar_valor_digitado`, 70-83).
5. Cópia limitada: blocos de 64 KiB até o teto + 1, SHA-256 e arquivo `0600` em diretório temporário próprio. O `Content-Length` não é autoridade (`etl_uploads.ler_upload_validado`, 61-97).
6. A Baia faz a primeira triagem no processo web: a assinatura dos primeiros 8 KiB precisa ser coerente com a extensão (`security_gatekeeper.inspecionar_arquivo`, 217-237).
7. `run_validation`, com prazo de até 120 s, sobe o filho na operação `validate`, que roda `triar` (733-758). `triar` faz:
   - a leitura pelo motor, com neutralização;
   - a estrutura mínima de PDV;
   - o inventário de conteúdo ativo do XLSX;
   - se preciso, a cópia só com valores, conferida por releitura.

   A estrutura mínima (`exigir_estrutura_minima`, 522-533) não recebe o tipo declarado. O arquivo passa se parecer de vendas (`_minimo_vendas`: produto, e quantidade, valor ou preço unitário) ou de compras (`_minimo_compras`: produto ou fornecedor, e valor, custo unitário ou quantidade comprada). Assim, um relatório de compras enviado no campo VENDAS passa na Baia e é gravado. O processamento aplica depois as próprias regras, mais estritas, e uma recusa chega ao usuário com a mensagem genérica do processo isolado.
8. De volta ao pai, `security_gatekeeper.ler_liberado` (240-259) relê a cópia neutralizada, quando ela existe, e confere de novo o teto, o arquivo regular sem link e a assinatura. `etl_uploads.ler_upload_validado` troca os bytes e o SHA-256 pelos da cópia e registra a liberação ou a recusa no log (`etl_uploads.py` 98-113).
9. A rota revalida a análise com `FOR UPDATE`, grava o arquivo em `storage.salvar` e cria `UploadRelatorio` como PENDENTE (`blueprints/cliente.py` 669-715).

### 1.4 Fluxo 2: processamento da análise (operação `calculate`)

1. O consultor dispara `POST /admin/analise/<id>/processar`. A rota exige perfil de administrador e a vaga única (`blueprints/admin.py` 1389-1392).
2. A análise é travada com `FOR UPDATE`. O processamento é recusado se houver devolutiva publicada ou se faltar VENDAS ou COMPRAS. Depois, o status passa a `EM_ANALISE` (1395-1416).
3. Os dois arquivos são materializados em cópias privadas, conferindo o tamanho real do BYTEA (`storage.materializar_para_etl`, 145-220).
4. A chamada segue `processar_arquivos_analise` → `calcular_kpis_em_memoria` → `run_calculation` → `_run_job`. O filho executa `_calcular_kpis_local` (`etl_processor.py` 887-1058).
5. **VENDAS.** `read_raw` e depois `_vendas_do_bruto` (629-681), que faz:
   - o cabeçalho;
   - o recorte canônico;
   - a situação (cancelamento e devolução);
   - o filtro de lixo;
   - a conversão de quantidade, valor, custo e estoque;
   - a chave do produto;
   - o descarte de não-produtos.
6. **COMPRAS.** `carregar_compras` (684-729) trata cabeçalho, chave por fornecedor ou produto e coluna de valor por prioridade, e descarta valores ≤ 0.
7. **Agregação por produto normalizado** (903-938):
   - faturamento líquido;
   - produto mais vendido;
   - produto de maior faturamento;
   - Curva ABC completa (784-845).
8. **Período.** O arquivo de VENDAS é **relido** do disco só para achar "Período" no cabeçalho (941; `_detectar_periodo_arquivo` 773-778). Se não achar, relê COMPRAS.
9. **Compras.** Calcula o total comprado, o Indicador de Pressão de Estoque (comprado − faturado) e o produto com maior saldo parado, este só com compras item a item (946-983).
10. Monta a telemetria, os alertas e o dicionário de 12 KPIs (985-1058).
11. O retorno do filho passa pelo teto de 4 MiB, e o pai valida campos e tipos (`etl_runtime.py` 135-146).
12. **Persistência num SAVEPOINT** (1173-1187):
    - upsert de `indicador_analise` com `versao_processamento + 1`;
    - DELETE e INSERT em lote da Curva ABC inteira (1108-1159);
    - em erro, nada é gravado e os dados anteriores ficam.
13. **Na mesma transação**, os uploads passam a PROCESSADO e roda a conferência do total do PDV: `validacao_pdv.conferir_e_registrar` → `validar_total_pdv`, com tolerância de R$ 1,00 (`blueprints/admin.py` 1434-1452).
14. O cliente recebe um aviso sem valores. Se a conferência der `DIVERGENTE_PDV`, o aviso fala em diferença no total (1459-1478).
15. A publicação é humana. Só `admin.analise_relatorio` grava `CONCLUIDO`, e só depois de reler o motivo de bloqueio sob o lock (1232-1306).
16. Consumo: painel (Pareto Top 10, ABC completa), semáforos (`insights.gerar_semaforos`), PDF (Top 5), devolutiva por IA e cobertura contratual.

### 1.5 Fluxo 3: Raio-X do PDV (operação `raio_x`)

1. O cliente envia de 1 a 36 relatórios mensais de vendas, pela página ou pela API, com:
   - limite de 10 envios por hora;
   - a funcionalidade `raio_x_entrada` do plano;
   - a vaga única (`blueprints/onboarding.py` 39, 142-162 e 212-229).
2. `raio_x_servico.processar_envio` (69-110) prepara cada arquivo:
   - extensão csv ou xlsx;
   - cópia limitada (`_gravar_limitado`, 47-66);
   - assinatura da Baia com o rótulo "Arquivo N";
   - recusa de arquivo repetido pelo SHA-256 (102-104).
3. `etl_runtime.run_raio_x` (117-128) usa prazo de `min(120, 2 × 60)` s para **todos os arquivos juntos** e exige exatamente as chaves `CHAVES_RESULTADO`.
4. No filho, primeiro roda `validate_input` em cada arquivo, que confere estrutura e orçamento sem montar a matriz. Depois roda `raio_x.diagnosticar` (397-506).
5. `_ler_arquivo` (154-271) lê cada arquivo **uma única vez**:
   - `_ler_bruto` → `read_raw`;
   - o período sai da mesma matriz;
   - a limpeza é a herdada de `_vendas_do_bruto`;
   - depois vêm as regras próprias por linha e a agregação por produto do arquivo.
6. A combinação (407-442) ordena os arquivos por período, soma por produto e usa nome, código e referência do mês mais recente. Ela calcula:
   - o índice de saúde;
   - a margem;
   - a precificação;
   - as listas top 50;
   - os avisos.
7. No pai:
   - relê a situação contratual (`onboarding.py` 119-122);
   - recomenda o plano (`raio_x.recomendar`, 512-544);
   - grava uma linha por empresa em `diagnostico_entrada` (`raio_x_servico.salvar`).

   Os arquivos somem com o diretório temporário, e **nenhuma linha de venda é persistida**.
8. Na leitura, `visao` (173-216) tira o que o plano não libera e oculta fatias além do histórico do plano.

### 1.6 Formatos, codificações e padrões de relatório

**Formatos de arquivo**

| Formato | Situação | Onde |
|---|---|---|
| `.csv` | Aceito em toda a cadeia | `config.py` 275-277; `etl_inputs._run` 601-622 |
| `.xlsx` | Aceito. ZIP sem criptografia, com as partes obrigatórias e ao menos uma aba. Um `.xlsm` renomeado vira cópia só com valores. | `etl_inputs._zip_preflight` 479-517; `security_gatekeeper.inventariar_xlsx` 480-510 |
| `.xls` (Excel 97-2003, ou HTML e XML salvos como `.xls`) | **Recusado pela extensão**, antes da Baia. No portal: "Extensão .xls não suportada". No Raio-X: "Arquivo N: formato não suportado. Envie CSV ou XLSX." Não há leitor BIFF (o `xlrd` saiu do projeto). | `blueprints/cliente.py` 657-663; `blueprints/admin.py` 999-1005; `etl_uploads.py` 75-76; `raio_x_servico.py` 87-90 |
| `.txt` | **Recusado pela extensão** nas rotas e no leitor. A Baia o reconheceria como texto (`FORMATO_DE_LEITURA` txt→csv, linha 64), mas as rotas recusam antes. | `config.py` 277; `raio_x_servico.py` 88-90 |
| Excel 97-2003, XML ou HTML **renomeado** para `.csv` ou `.xlsx` | Recusado pela Baia na assinatura. Planilha OLE2 vira `xls_disfarcado`, com orientação para salvar como `.xlsx` ou `.csv`. XML ou HTML vira "marcação", sem orientação de conversão, o que afeta relatórios exportados por PDV web. | `security_gatekeeper._ole_e_planilha` 163; `_INICIOS_PROIBIDOS` 127; `identificar_conteudo` 190-214 |

**Codificação (só CSV).** É uma cascata fixa, sem detecção estatística: `ENCODINGS = ('utf-8-sig', 'cp1252', 'utf-8', 'latin1')` (`etl_inputs.py` 26; `_csv_input` 148-172). A primeira codificação que decodifica o arquivo inteiro e produz mais de uma coluna vence.

- **UTF-8, com ou sem BOM.** Lido como `utf-8-sig`, e o BOM é consumido.
- **Windows-1252, a exportação típica de PDV no Windows.** Lido como `cp1252`.
- **ISO-8859-1 (latin1).** Só é alcançado quando o cp1252 falha nos bytes 0x81, 0x8D, 0x8F, 0x90 ou 0x9D. Como o latin1 decodifica qualquer byte, ele nunca falha.
- **Armadilha medida 1: UTF-8 com um único byte cp1252 perdido.** O arquivo inteiro cai para cp1252: "Café" vira "CafÃ©", e a chave do produto vira "CAFA". Num lote de 36 meses, o mesmo produto pode se dividir em duas chaves, sem aviso.
- **Armadilha medida 2: "Texto Unicode" do Excel (UTF-16).** Funciona por acidente: é decodificado como cp1252 e os bytes NUL são removidos linha a linha (`_CSVLines.__next__`, 100-101). Os acentos latinos saem certos, mas caracteres fora do Latin-1 viram controle. Não há decodificação UTF-16 de verdade.

**Separador.** `SEPARATORS = (';', ',', TAB, '|')` (linha 27). O leitor percorre o arquivo inteiro com cada separador, e vence o que der mais colunas. No empate, vale a ordem da tupla. Outros separadores, como `:` e largura fixa, resultam em `csv_invalid`. LF, CRLF e CR sozinho são aceitos.

**XLSX**
- Só uma aba é lida: a que tem mais células preenchidas, inclusive oculta (`_xlsx_input` 551-582). **Um XLSX com uma aba por mês perde todas as outras abas sem aviso.**
- Vale o valor em cache das fórmulas (`data_only=True`).
- Números são lidos como texto (`dtype=str`). `_disambiguate_numbers` (530-545) separa um número 12,125 do texto "12.125", que pode ser milhar.

**Cabeçalho.** O motor não depende da posição nem do nome exato das colunas:
- **Busca.** Procura nas primeiras 100 linhas a linha que mapeia mais colunas canônicas (`_detectar_cabecalho`, 437-486).
- **Cabeçalho composto.** Se nenhuma linha sozinha serve, tenta duas linhas adjacentes concatenadas.
- **Casamento.** É por fragmento contido no título normalizado (`_norm_chave` 102-117; `_mapear_cabecalho` 415-434).
- **Aviso.** A docstring do módulo (linha 13) ainda fala em 60 linhas; o código usa 100.

| Canônico (vendas) | Títulos reconhecidos (medido) | Títulos comuns **não** reconhecidos (medido) |
|---|---|---|
| produto | "Descrição do Produto", "Descrição", "Produto", "Mercadoria" | — |
| codigo | "Código", "Cod. Barras", "EAN", "Referência", "SKU" | "Cód.", "Cód", "ID" |
| quantidade | "Qtde.", "Qtd", "Quantidade", "Qtde Vendida" | "Quant.", "Un" |
| valor_venda | "Valor Total", "Vr. Total", "Faturamento", "Total Líquido", "Total Venda" | "Vlr. Total", "Vlr Total", "Vl. Total", "Total", "Total R$", "Valor", "Valor Líquido", "Valor Bruto", "Receita", "Vendas" |
| preco_unitario | "Preço Unitário", "Valor Unitário", "Preço Venda" | "Vr. Unit.", "Vlr. Unit." |
| valor_custo | "Total Custo", "Custo Unitário", "Custo Médio", "Preço de Custo" | "CMV" |
| estoque | "Qtde Estoque", "Estoque Atual" | — |

Fonte: `ALIAS_VENDAS` (`etl_processor.py` 359-387) e `ALIAS_COMPRAS` (389-412), medidos com a cópia do `91e5fb0` (itens F-03 a F-05 do anexo).

Em compras faltam, por exemplo, "Quantidade", "Qtd", "Custo Total" e "Vlr Total". O casamento por fragmento também gera falsos positivos. Os dicionários de aliases estão iguais desde o primeiro commit do motor (`fc63708`).

**Colunas de situação**
- **Leitura à parte.** "Status", "Situação", "Operação", "Natureza da Operação", "Tipo Movimento" e afins são lidas fora dos aliases, para não disputar colunas (`_RE_COLUNA_SITUACAO`, 544-550).
- **Cancelamento.** Palavra que começa com CANC, ESTORN ou INUTILIZ: a linha sai.
- **Devolução.** Palavra que começa com DEVOL ou TROCA: a linha fica, com quantidade e valor negativos.
- **Negação.** "NÃO " ou "SEM " imediatamente antes anula a marca (553-601).
- **Assunto diferente.** Colunas como "Status Pagamento" são recusadas por tratarem de outro assunto.

**Período do relatório**
- Lido só do cabeçalho: nas primeiras 30 linhas, uma célula com a palavra "período" e datas `dd/mm/aa` ou `dd/mm/aaaa` (`_extrair_periodo_base`, 751-770).
- **Não reconhecidos (medido):**
  - datas em células separadas;
  - ISO (`2026-01-02`);
  - separador `-` ou `.`;
  - "De ... até ..." sem a palavra "período";
  - mês/ano;
  - período depois da linha 30.
- Ano de 2 dígitos vira 20xx: "99" vira 2099.
- Uma data de emissão na mesma célula estende o fim do período.

**Padrões de relatório que o motor espera**
- **VENDAS.** Um relatório "vendas por produto", com uma linha por produto no período. Também aceita uma linha por venda, desde que o relatório tenha produto, quantidade e (total ou preço unitário). A estrutura mínima está em `_validar_vendas` (607-612).
- **COMPRAS.** Por nota fiscal (fornecedor e valor) ou por item (produto, quantidade e total ou custo unitário). O formato por nota fiscal não permite calcular o saldo parado por produto (`_validar_compras` 615-622; R-41 do anexo).
- **Raio-X.** Só relatórios de vendas por produto, de preferência um por mês.
- **Não há data por linha de venda.** `ALIAS_VENDAS` não tem o canônico "data".

---

## 2. Regras de negócio e limpeza

### 2.1 Higienização

**Moeda (valores em dinheiro)**
- **Limpeza** (`_limpar_monetario`, 222-226):
  - os sinais "−", "–", "﹣" e "－" viram "-";
  - remove controles, espaços, NBSP e caracteres de largura zero (categorias Unicode C e Z);
  - remove aspas e apóstrofos das pontas;
  - remove "R$", "US$", "BRL", "USD" e "$".
- **Sinal** (`_separar_sinal`, 229-238). "(10,00)", "-10" e "10,00-" são negativos; o último é o sinal contábil de PDV.
- **Separadores** (`_normalizar_separadores`, 241-260):
  - com vírgula e ponto, o mais à direita é o decimal;
  - pontos repetidos são milhar;
  - notação científica é aceita.
- **Decisão por coluna** (`_coluna_usa_ponto_decimal`, 296-305). Se a coluna tem algum valor com ponto que não pode ser milhar ("12.50") e nenhum com vírgula decimal, a coluna inteira usa ponto decimal, e "12.500" passa a valer 12,5.
- **Qualidade** (`_converter_serie_monetaria`, 308-322):
  - valor inválido vira R$ 0,00 e é contado;
  - "12.500" lido como milhar ou como decimal também é contado;
  - os dois geram alerta na análise (`_alertas_de_valores`, 848-881);
  - o Raio-X descarta esses contadores.
- **Lacuna.** O milhar en-US com vírgula ("12,500") é lido como 12,5 sem alerta nenhum.

**Quantidade e estoque** (`_parse_valor`, 156-202; `_converter_serie` 205-206)
- Ponto único é decimal: "1.500" vale 1,5 kg.
- Vírgula é decimal.
- Unidades e texto são descartados: "3 kg" vira 3; "2 un(s)" vira 2.
- "(2)" e "-2" são negativos.
- Nulo ou irrecuperável vira 0.
- **Defeitos medidos, que o conversor de dinheiro não tem:**
  - "2-" vira +2;
  - "−2" (menos Unicode) vira +2;
  - "1e3" vira 13;
  - "1/2" vira 12;
  - "2026-01-02" vira 20260102.

**Apóstrofos e fórmulas**
- **Neutralização.** Toda leitura do motor sai neutralizada: a célula que seria fórmula ganha um apóstrofo no início (`security_gatekeeper.neutralizar_celula` 327-351, chamada por `etl_inputs.read_raw` 625-633).
- **O que conta como fórmula** (`_formula_no_inicio`, 303-311):
  - começo em `=` ou `@`, sempre;
  - começo em `+` ou `-` só quando vem depois algo que só fórmula tem (`_SINAL_DE_FORMULA`, 295): operador, referência externa, DDE, aspas ou chamada de função;
  - números com sinal ("-12,50", "-1 UN", "+5%", "-1,5E+03") e o sinal contábil no fim ("10,00-") ficam como estão.
- **Dentro da célula** (`_neutralizar_trechos`, 314-324). Depois de `;`, `,`, TAB ou `|` seguido de fórmula, o apóstrofo entra no meio: "Kit,=2" vira "Kit,'=2". Cada trecho é julgado até o próximo separador do mesmo tipo.
- **Brancos e BOM.** TAB, CR e LF no início viram espaço, mesmo sem fórmula. Restos de BOM (`_RESTOS_DE_BOM`, 290) são mantidos, e a célula é julgada a partir do que vem depois deles.
- **Efeito no portal.** Se alguma célula mudou (`attrs['celulas_neutralizadas']`, contado em `neutralizar_matriz` 359-379), a Baia grava a cópia só com valores em vez do original (`triar`, 750-751).
- **Números.** Não mudam, porque `_parse_valor` (170-171) e `_limpar_monetario` ignoram o apóstrofo inicial.
- **Nomes.** O efeito visível fica só no nome do produto: "'=== PROMO ===".
- **Código do produto.** O apóstrofo também fica no código (`raio_x._codigos` não o remove).

**Datas**
- Só o período do cabeçalho é lido (`_RE_DATA_BR` 738; `_parse_data_br` 741-748; `_extrair_periodo_base` 751-770).
- Em COMPRAS, a coluna "data" é mapeada, mas nunca convertida nem usada.
- No XLSX, uma data vira o texto "2026-01-02 00:00:00".

**Nulos**
- **CSV:** não há conversão de nulos. "NA", "NULL" e "None" continuam texto, e campo vazio é "" (`_csv_input` 183-184).
- **XLSX:** valem os `STR_NA_VALUES` do pandas, e "#N/A", "NULL" e "NA" viram ausentes (`_xlsx_input` 587-595).
- **Na coluna-chave:** vazio, "nan" e "none" descartam a linha (`_filtrar_linhas_validas` 524-535).

**Texto e nomes**
- **Chave de cabeçalho** (`_norm_chave`, 102-117): sem acento, minúscula, só letras, dígitos e espaço.
- **Chave do produto** (`_normalizar_produto`, 120-125): maiúscula, sem acento e sem pontuação. "Café Pilão 500g" e "CAFE PILAO 500G" viram o mesmo produto.
- **Nome exibido** (`_nome_exibivel`, 133-139): corta em 200 caracteres e tira o NUL. Quem escolhe o nome é quem chama a função. Na análise mensal, é o primeiro nome do grupo (907). No Raio-X, é o primeiro de cada arquivo (`raio_x.py` 237) e, no envio combinado, o do mês mais recente (415).

**Linhas que não são produto**
- **`_PADRAO_LIXO`** (493-497) descarta a linha se a coluna-chave contém, como palavra inteira:
  - total geral, totais, subtotal, movimenta, labsofti, software;
  - resumo, periodo, usuario, filtro, impresso, pagina.
- **`_CABECALHOS_REPETIDOS`** (501-505) descarta cabeçalhos repetidos nas quebras de página.
- **`_NAO_PRODUTOS`** (146-150) compara por igualdade exata com o nome normalizado:
  - DESCONTO, DESCONTOS, ACRESCIMO, ACRESCIMOS, FRETE, TROCO, TAXA, TAXAS, JUROS;
  - TOTAL, SUBTOTAL, TOTAL GERAL;
  - ARREDONDAMENTO, ENTRADA, SANGRIA, SUPRIMENTO.

### 2.2 Detecção de imperfeições do PDV

| Imperfeição | Análise mensal (`etl_processor`) | Raio-X (`raio_x`) | Situação |
|---|---|---|---|
| **Venda abaixo do custo** | Conta **linhas** com `valor_custo > 0` e `valor_venda < valor_custo`, sem devoluções. Gera alerta de "sinal bruto, não margem" (`_calcular_kpis_local` 1009-1022). | Por linha com custo: perda = max(custo − faturamento, 0), com custo unitário multiplicado pela quantidade. Conta **produtos**, com quantidade, faturamento, custo e perda (`_ler_arquivo` 223-228). | **Divergem.** A análise não sabe se a coluna de custo é unitária ou total: o alias genérico "custo" pega as duas (`ALIAS_VENDAS` 370-371). Com custo por unidade, compara unitário com o total da linha e subestima; com "Total Custo", acerta. O Raio-X decide pelo título (`custo_e_unitario`, 85-91). |
| **Custo R$ 0,00** | Não detecta: o alerta exige custo > 0. | "Sem custo informado" para custo vazio, zero, inválido ou negativo (`_ler_arquivo` 171-204). O índice de saúde só considera o faturamento com custo. Faturamento sem custo ≥ 10% (`LIMIAR_SEM_CUSTO`) é motivo de recomendação. | Só o Raio-X. |
| **Omissão de relatório consolidado do ERP** | Não existe. | Também não existe como detecção. O "scanner de omissão" marca itens com quantidade > 1 vendidos abaixo do preço de referência, ou com preços diferentes no mesmo arquivo. A diferença precisa passar de max(R$ 0,01; 0,5%) (`_ler_arquivo` 205-222; `TOLERANCIA_*` 65-68). O sinal de que o próprio relatório já veio consolidado é `variacao_visivel` (264-265), que só gera uma nota na tela. | **Lacuna.** `flag_omissao_erp` não é calculada. |
| **Defasagem de margem bruta** | Proibida por regra: o motor nunca expõe margem, lucro ou CMV (docstring 22-35). | **Precificação desatualizada** (`_precificacao`, 338-372). Entre o primeiro e o último mês com custo, o custo unitário subiu ≥ 5% (`LIMIAR_ALTA_CUSTO`) e o preço médio subiu menos que o custo menos 2 pontos (`FOLGA_REPASSE`). Também calcula a **margem bruta estimada**, só nas vendas com custo, e a **margem negativa** no período (432). A margem bruta estimada e a precificação desatualizada são liberadas a partir do PRATA; a contagem de itens com margem negativa aparece em todos os planos. | Só o Raio-X. A análise mensal não tem. |
| **Estoque negativo** | Conta **linhas** com estoque < 0 e alerta acima de 10% das linhas (996-1007). | Conta **produtos** distintos com estoque mínimo < 0, sem limiar (187-188). | Divergem. |
| **Venda sem valor** | — | Linhas com quantidade > 0 e valor ≤ 0 (263). | Só o Raio-X. |
| **Código com vários nomes, ou nome com vários códigos** | Ignorado: o agrupamento é só pelo nome. | `_conflitos` (139-151), por arquivo. Os produtos envolvidos saem do faturamento auditável. | Só o Raio-X, e só dentro de cada arquivo. |
| **Cancelamentos e devoluções** | Canceladas saem; devoluções são deduzidas do faturamento (`_aplicar_situacao_das_vendas` 575-601). | Herda a mesma regra e soma as devoluções à parte. Recalcula o total que o ETL já tem em `attrs['situacao']`. | Duplicação. |
| **Valor ilegível** | Contado e alertado (848-881). | Descartado. | Só a análise mensal. |
| **Total divergente do PDV** | **Trava do PDV**: o total digitado é comparado com o faturamento bruto calculado, com tolerância de R$ 1,00 em centavos exatos. Uma divergência bloqueia a publicação (`em_centavos` 1064-1069; `validar_total_pdv` 1072-1087; `validacao_pdv.py`). | Não confere total nenhum. | Só a análise mensal. |

**Outros sinais do Raio-X, todos em `raio_x.py`:**
- **Índice de saúde cadastral, de 0 a 100.** É a parte do faturamento analisado que tem custo informado e cujo produto não tem conflito de código ou nome (452).
- **Potencial recuperável por linha.** É max(referência × quantidade − valor; custo − valor). Assim a mesma diferença não conta duas vezes (224 e 495-499).
- **Recomendação de plano** (`recomendar`, 512-544). Tem quatro motivos:
  - nenhum arquivo com custo;
  - ≥ 10% do faturamento sem custo;
  - margem negativa;
  - recuperável ≥ 1% do faturamento.

  Sem motivo, a recomendação é BRONZE. Com motivo e envio de mais de 24 meses, é sempre OURO, e o PRATA nunca aparece para 3 anos.
- **O que cada plano vê** (`raio_x_servico.visao`, 173-216). A margem bruta, a lista de itens abaixo do custo, o custo total das vendas e a precificação desatualizada exigem o PRATA. O índice, as contagens (inclusive de itens com margem negativa e abaixo do custo), a perda total abaixo do custo e o recuperável aparecem em todos os planos.

### 2.3 Estruturas de processamento: pontos fortes e limitações

**Como é hoje**
- **pandas 3.0.3.** O XLSX é lido com `pd.read_excel(dtype=str)`, que no pandas 3 vira o tipo `str` (`future.infer_string`). O CSV vira `DataFrame(dtype=object)` (`etl_inputs.py` 183-184 e 591). Nos dois casos, a matriz inteira fica em memória, como texto.
- **Leitura em memória.** Não há leitura em blocos nem streaming: `read_raw` sempre devolve a aba ou o CSV inteiro.
- **Execução síncrona.** O job roda dentro da requisição HTTP, que espera o subprocesso. Não há fila, execução assíncrona nem pool.
- **Sem paralelismo.** Os arquivos do Raio-X são lidos um por vez, no mesmo processo.
- **Uma vaga por processo** e um processo por servidor.

**Limites que valem para qualquer abordagem**

| Limite | Valor | Onde |
|---|---|---|
| Tamanho do arquivo | 20 MiB, com teto absoluto de 20 MiB e 50 MiB na Baia | `etl_limits.ETLLimits` (9-41); `security_gatekeeper.teto_bytes` (104) |
| Corpo da requisição | 20 MiB somados: os 36 meses do Raio-X dividem esse total | `config.py` 227-229 |
| Linhas e colunas | 100 mil linhas e 128 colunas | `ETLLimits` |
| Células | 500 mil por aba ou CSV, 1 milhão por arquivo | `ETLLimits`; `etl_inputs._csv_pass` 117-123 |
| Prazo do job | 60 s no `calculate`, 120 s no `validate` e no `raio_x` | `etl_runtime.py` 106-131 |
| Memória do filho | 384 MiB de espaço de endereçamento | `etl_limits.py` 23; `etl_worker.py` 24-33 |
| Saída do filho | 4 MiB, teto rígido; acima disso o erro é `output` | `etl_limits.py` 25 e 51; `etl_worker.py` 94-96 |
| Pedido ao filho | 16 KiB, só caminhos e limites, nunca dados | `etl_runtime.py` 57-60 |

**Medições de volume** (CSV mensal sintético em cp1252, 7 colunas; medido no próprio processo, neste PC)

| Cenário | Tempo | Observação |
|---|---|---|
| Raio-X, 12 meses × 2.000 produtos (CSV) | 5,7 s | — |
| Raio-X, 36 × 2.000 (CSV) | 15,8 a 16,4 s | — |
| Raio-X, 12 × 5.000 (CSV) | 13,0 s | — |
| Raio-X, 36 × 5.000 (CSV) | 34,1 a 36,6 s | 47% em `_vendas_do_bruto`; cada valor em dinheiro é limpo duas vezes |
| Raio-X, 36 × 2.000 (XLSX) | 75,6 s | — |
| Raio-X, 36 × 5.000 (XLSX) | **173 s** | Acima dos 120 s do prazo |
| Import a frio do filho | cerca de 2,2 s por job | pandas, openpyxl e sqlalchemy; o sqlalchemy só entra porque `etl_processor.py` o importa no topo |

- **Memória.** O pico do Raio-X de 36 × 5.000 foi 184 MiB de heap Python. Somados os cerca de 176 MiB dos imports, fica perto dos 384 MiB do limite. É estimativa: precisa ser medido em Linux.
- **Arquivo único de 3 anos.** Esbarra nas 500 mil células. Com 7 colunas, cabem cerca de 71 mil linhas. Um consolidado de 36 × 2.000 foi recusado com 504.042 células.
- **Saída da análise mensal.** A Curva ABC em JSON ocupa cerca de 175 B por produto, o que dá teto prático de cerca de 24 mil produtos distintos por análise.
- **Saída de linhas produto × ano.** Em lista de dicionários, 15 mil linhas (5.000 produtos × 3 anos) passam de 4 MiB. Em formato colunar, cabem cerca de 24 mil a 36 mil linhas, conforme as colunas.
- **Render.** O `render.yaml` declara o plano gratuito, com 0,1 CPU. A estimativa é de tempo cerca de 10 vezes maior: até o Raio-X CSV 36 × 2.000 passaria dos 120 s.
- **Vaga única.** Durante um Raio-X de até 120 s, todo upload ou processamento de qualquer cliente recebe "Há outro upload ou processamento em andamento" (`etl_uploads.py` 28-29). Todo Raio-X recebe "Há outro envio ou processamento em andamento" (`blueprints/onboarding.py` 41), com status 503 na API.

**Pontos fortes**
- **Isolamento sólido.** O filho não tem banco, rede, subprocesso nem segredos, e funciona sob limites de memória, CPU, tamanho de arquivo e saída. Em qualquer falha, o resultado é recusa, nunca dado falso (`etl_worker.py`).
- **ZIP e XML validados em fluxo**, com memória constante. Bomba de compressão, entidade XML e coordenada esparsa são recusadas antes de alocar memória (`etl_inputs._zip_preflight` e `_XMLCheck`).
- **Leitura única**, compartilhada por análise, Raio-X e Baia, e **limpeza única**, compartilhada por análise e Raio-X.
- **Golden de equivalência**, capturado do motor de `ff14879` (antes do SEC-005) e revisado uma vez em `0ac01c8` (seção 6), e testes de regras financeiras. Os números são:
  - `tests/test_etl_financial_equivalence.py`: 15 testes;
  - `tests/test_etl_regras_financeiras.py`: 46;
  - `tests/test_planos_raio_x.py`: 62;
  - `tests/test_security_gatekeeper.py`: 40.
- **Gravação atômica** num SAVEPOINT. Em erro, os dados anteriores ficam.

**Limitações**
- Tempo e memória crescem linearmente com meses × produtos, e a limpeza é feita célula a célula em Python.
- O mesmo arquivo é lido mais de uma vez:
  - no Raio-X, `validate_input` seguido de `read_raw`: cerca de 11% do tempo no CSV e 25% no XLSX;
  - no `calculate`, as VENDAS são relidas inteiras só para achar o período (19% do tempo no CSV de 12 meses e 47% no XLSX), e as COMPRAS também, quando as vendas não têm período (`etl_processor.py` 941);
  - no XLSX, `_disambiguate_numbers` relê a aba quando há número com cara de milhar (`etl_inputs.py` 530-545).
- A vaga única sem fila transforma qualquer job longo em indisponibilidade para todos os clientes.

**Ganhos medidos sem mudar nenhuma regra** (oportunidades, não implementadas):
- eliminar as releituras;
- limpar cada valor em dinheiro uma vez só;
- memorizar `_norm_chave` e `_normalizar_produto` por valor distinto, já que 5.000 nomes se repetem 36 vezes;
- liberar a lista de partes antes do concat no Raio-X.

Cada otimização deve entrar em commit próprio, conferido pelo golden de equivalência e pelos testes de regras.

---

## 3. O papel do `raio_x_servico.py`

### 3.1 Responsabilidade exata

O `raio_x_servico.py` é o **orquestrador e a camada de produto** do Raio-X. Ele roda no processo web e **não limpa nem interpreta nenhum dado**.

| Função | Linhas | O que faz |
|---|---|---|
| `_gravar_limitado` | 47-66 | Copia cada arquivo em blocos de 64 KiB até `teto_bytes` + 1, com SHA-256, permissão `0600` e recusa de arquivo vazio |
| `processar_envio` | 69-110 | Descarta campos vazios e exige de 1 a 36 arquivos. Faz `secure_filename`, aceita só csv ou xlsx e grava como `arquivo_NN.<ext>` num diretório temporário. Chama a 1ª triagem da Baia, recusa SHA-256 repetido e chama `etl_runtime.run_raio_x` |
| `salvar` | 116-135 | Upsert com `FOR UPDATE` da linha da empresa em `diagnostico_entrada`, sem commit. Grava índice, recuperável, plano recomendado, versão das regras, período e o JSON completo |
| `ultimo` | 138-150 | Lê o JSON gravado e exige as chaves do resultado |
| `visao`, `_corte_historico` e `mostra_todas_as_fatias` | 156-216 | Tiram do resultado o que o plano não libera (margem a partir do PRATA, auditoria abaixo do custo) e ocultam fatias além do histórico do plano (6 meses no BRONZE, 24 no PRATA) |

O módulo também tem `CAMPOS_FATIA` (39), que é código morto: nada usa essa constante.

### 3.2 É o motor ou depende de outros serviços?

**Não é o motor.** A cadeia completa de dependência é:

```
blueprints/onboarding.py → raio_x_servico → etl_runtime → etl_worker → raio_x → etl_processor → etl_inputs → security_gatekeeper
```

- **`raio_x.py` é o motor de regras do Raio-X.** Ele também não higieniza: importa do `etl_processor` as funções `_ler_bruto`, `_vendas_do_bruto`, `_extrair_periodo_base`, `_converter_serie_monetaria` e `_nome_exibivel` (`raio_x.py` 57-60).
- **Origem dessa ligação.** O commit `ec4ba95` extraiu `_vendas_do_bruto` de `carregar_vendas`, sem mudar comportamento, justamente para o Raio-X reaproveitar a limpeza.
- **`recomendar` roda no processo pai**, porque depende do catálogo de planos (`onboarding.py` 123-124).

### 3.3 Ligação com a Baia de Segurança

A Baia (`security_gatekeeper.py`) entra no Raio-X por três pontos:

1. **Teto de bytes.** `teto_bytes` define o limite da cópia em `_gravar_limitado` (`raio_x_servico.py` 50).
2. **Primeira triagem.** `inspecionar_arquivo` confere a assinatura dos primeiros 8 KiB com o rótulo "Arquivo N" (`raio_x_servico.py` 93-101). Uma recusa vai para `registrar_recusa(origem='raio_x')` (`security_gatekeeper.py` 265-272), que escreve uma linha de log (`logger.warning`, não no banco) com origem, motivo, extensão, tamanho e SHA-256, nunca o conteúdo, o nome nem o caminho.
3. **Neutralização.** É aplicada a cada leitura, dentro de `etl_inputs.read_raw`, no processo isolado.

O Raio-X **não** chama `triar`: não há estrutura mínima, inventário do XLSX nem cópia só com valores. É uma decisão de projeto, registrada em `raio_x_servico.py` 93-95: os arquivos não são guardados nem redistribuídos.

### 3.4 Ligação com `lead_avaliacao` e `brainstorming_dados_pdv`

**Não há ligação nenhuma.**
- **Onde as tabelas existem:**
  - `models.py` (1302-1402);
  - a migração `f4c2a8e1d9b6`;
  - `leads_avaliacao.py`, que tem o expurgo e o comando `flask leads`, registrado em `app.py` 292-294;
  - os testes e a documentação.
- **Quem não as importa:** `raio_x_servico.py`, `raio_x.py`, `etl_processor.py` e `security_gatekeeper.py`.
- **Estado.** Nenhum código insere linhas nelas. O único que as altera é o expurgo, que marca `expirado` e apaga as linhas vencidas (`leads_avaliacao.py` 68-78). Os status `em_processamento`, `concluido`, `processamento_manual` e `convertido` nunca são atribuídos.

O caminho mais próximo da Avaliação de Cenário é a cadeia do Raio-X:
- aceita só vendas;
- recebe até 36 arquivos;
- não guarda os arquivos;
- já produz o índice de saúde, o potencial recuperável e o plano recomendado.

Mas ela devolve só agregados e listas top 50, e não a matriz produto × ano. Além disso, atende só cliente logado, com plano e contrato ativo.

**Mapeamento possível dos campos (não implementado):**

| Coluna | De onde pode vir | Observação |
|---|---|---|
| `lead_avaliacao.score_saude_cadastral` | `saude.indice` (`raio_x.py` 452) | Pronto |
| `lead_avaliacao.vazamento_total_r$` | `recuperavel.total` (495-499) ou soma das linhas | Decisão do dono: os dois diferem em centavos |
| `lead_avaliacao.plano_recomendado_codigo` | `raio_x.recomendar` (512-544) | Pronto. Com 36 meses, qualquer problema leva a OURO |
| `brainstorming_dados_pdv.descricao_produto` | `_nome_exibivel` (`etl_processor.py` 133-139), 200 caracteres | Pronto, igual a `String(200)` |
| `codigo_produto` | `raio_x._codigos` (132-136), 60 caracteres | Pronto, igual a `String(60)` |
| `quantidade`, `valor_venda_bruto` | Somas do Raio-X por produto | `valor_venda_bruto` tem duas definições no código: a do Raio-X e a da trava do PDV (`faturamento_bruto`, 1090-1092) |
| `custo_medio_registrado` | custo total ÷ quantidade com custo | Hoje é arredondado a 2 casas (`_item`, 325-326). A coluna tem 4 |
| `flag_venda_sem_custo` | Produto com faturamento sem custo | Decidir se a regra é por item ou por faturamento |
| `flag_venda_abaixo_custo` | perda > 0 (`_ler_arquivo` 223) ou margem negativa (432) | Há três versões de "abaixo do custo" no código |
| `vazamento_estimado_r$` | Recuperável por produto (224), ou só a parte de preço (221-222) | Decisão do dono |
| `ano_exercicio` | **Não existe** | Só pode sair do período do cabeçalho de cada arquivo |
| `flag_omissao_erp` | **Não existe** | O proxy de variação contradiz `Docs/PLANOS_E_RAIO_X.md` 78 |

---

## 4. Diagnóstico de arquitetura: Abordagem A × Abordagem B

### 4.1 O que cada abordagem significa neste código

**Abordagem A (Motor Único Parametrizado)**
- **Na forma literal, não se sustenta.** A proposta é pôr `modo='brainstorming_lead'` ou `'producao_portal'` no motor e alternar só o destino da gravação. Mas:
  - o motor não grava nada: o processo isolado não tem banco, por desenho (`etl_worker.py` linha 1, *audit hook* em 35-47; fixado por `tests/test_planos_raio_x.py` 248-254), e quem grava é o processo pai;
  - o lead não pede só outro destino: pede outra saída (linhas produto × ano), regras que ainda não existem e outro contrato.
- **Pôr `modo=` em `processar_arquivos_analise` ou `_calcular_kpis_local` é indefensável:**
  - a assinatura é declarada imutável (`etl_processor.py` 37-39);
  - a operação `calculate` exige vendas e compras (`etl_worker.py` 57);
  - a Curva ABC e a Pressão de Estoque não servem ao lead;
  - o alerta de custo da análise não trata custo unitário.
- **Forma viável:** uma **operação nova do processo isolado**, `avaliacao_cenario`, ao lado de `validate`, `calculate` e `raio_x`. Ela reaproveita o motor do Raio-X. É o mesmo padrão do commit `ec4ba95`, que criou a operação `raio_x` com 9 linhas no worker e 14 no runtime.

**Abordagem B (Pipeline em Camadas)**
- **Na forma literal, também não se sustenta**, por três motivos:
  1. **Higienizar no `raio_x_servico.py` viola o SEC-005.** Ele roda no processo web, e higienizar ali poria pandas e openpyxl para ler arquivo hostil sem limites de memória e CPU e sem o *audit hook*.
  2. **`brainstorming_dados_pdv` não é uma staging de linhas brutas.** É uma matriz produto × ano, sem data, preço, situação nem arquivo por linha. As regras por linha do Raio-X (custo unitário, centavos, referência, vazamento, perda, recuperável) precisam rodar **antes** de agregar e não podem ser refeitas a partir do agregado. Além disso, a saída do processo isolado tem teto de 4 MiB.
  3. **O motor não lê banco.** Ele só consome a matriz de texto do `read_raw`, e o processo isolado não tem banco.
- **Forma viável:** o mesmo estágio 1 de A, mais uma triagem nova no processo pai que lê a staging. O único ganho próprio de B são os **lotes** (um envio por ano), que cabem no prazo e no limite da requisição. O custo é perder fidelidade (conflitos e precificação entre meses) e somar estados entre commits.

### 4.2 Comparação

| Critério | Abordagem A (forma viável) | Abordagem B (forma viável) | Vantagem |
|---|---|---|---|
| **Reaproveitamento** | 100% da leitura (`read_raw`), da limpeza (`_vendas_do_bruto`) e das regras por linha do Raio-X (`_ler_arquivo`). A agregação produto × ano é um groupby sobre `por_arquivo` (`raio_x.py` 409-411). Código novo só para a regra do ano, as guardas e a saída colunar. | O estágio 1 reaproveita o mesmo. Acrescenta `_classificar_curva_abc` por ano no estágio 2. Precisa de código novo para estados, lotes e recomposição do resumo. | A, por pouco |
| **Risco de duplicar ou divergir regras** | Baixo, se o modo ficar nas bordas (operação do worker e serviço do pai). O índice e o recuperável do lead saem do mesmo `diagnosticar` do cliente, e o plano, do mesmo `recomendar`. | Alto no estágio 2. Recalcular margem, custo ou índice a partir da staging é uma segunda implementação do Raio-X. Medido: a margem deduzida do custo médio × quantidade erra o sinal (−25,00 contra +2,00 reais). | A |
| **Manutenção** | Uma correção de limpeza vale para análise, Raio-X e lead no mesmo commit, sob as mesmas baterias de teste. Contra: uma mudança feita para o lead altera os números dos clientes, e a limpeza não tem versão. | A triagem muda sem tocar o protocolo do processo isolado e pode ser refeita sem novo envio. Contra: o contrato é triplo (saída, esquema e triagem), com estados sem fila para retomar. | Híbrido |
| **Desempenho em 3 anos** | Mesmo custo do Raio-X: CSV 36 × 5.000 em cerca de 33 s, mais 0,33 s para as linhas. XLSX 36 × 5.000 em 173 s, acima do prazo. Saída colunar de cerca de 168 B por linha, com teto de cerca de 24.500 linhas. | Estágio 1 igual. Ganho só com lotes de 1 ano: XLSX 12 × 5.000 em 59,8 s, e o pico de memória cai de 184 para 63 MiB. Paga em fidelidade e estados. | Empate no envio único |
| **Fidelidade e reprocessamento** | Marcas congeladas no processo isolado. Os arquivos somem e o esquema atual não guarda as somas: mudar a definição de uma marca exige pedir os arquivos de novo. | A staging ampliada (somas, linhas, meses, variação, conflito) permite recalcular marcas e triagem por 30 dias. | B (o híbrido incorpora) |
| **Segurança, LGPD e TTL** | Uma transação, um lote e um único `criado_em`. Uma falha do processo isolado não deixa lead órfão. | A persistência é obrigatória. Lotes em dias diferentes expiram em dias diferentes, e o lead pode ficar preso em `em_processamento`. | A |
| **Testabilidade** | Equivalência com o Raio-X por construção. A refatoração se prova com os 62 testes e um golden do JSON. | Superfície maior: estados, retomada, lotes e equivalência própria do estágio 2. | A |
| **Esforço**, depois das decisões do dono | 10 a 16 dias | 12 a 19,5 dias, dos quais 3 a 5 só se pagam com lotes | A |

**O ponto decisivo foi medido.** O juiz quantizou a staging ampliada como ela seria gravada e comparou com o Raio-X em cinco envios, do teste de 2 arquivos até 36 × 5.000 produtos (15 mil linhas):
- **Batem exatamente:** índice de saúde, faturamento, itens vendidos, itens sem custo, % sem custo, margem negativa, venda abaixo do custo, variação, estoque negativo, plano recomendado e número de motivos.
- **Diferem só em centavos:** o recuperável e o vazamento, por arredondamento por linha (R$ 0,15 em R$ 3,19 milhões).
- **Quantização:** nenhuma marca muda entre o valor em ponto flutuante e o valor gravado (0 diferenças em 21.015 linhas).

Por isso, ler a staging ampliada não cria uma regra divergente: é uma projeção com prova automática de equivalência.

### 4.3 Recomendação: híbrido com núcleo na Abordagem A

**Princípio.** O "modo" vive só nas bordas: na operação do processo isolado e no serviço do processo pai. **Nunca** entra um `if modo` em `etl_inputs`, em `etl_processor` nem em `raio_x._ler_arquivo`. Toda diferença de negócio do lead fica em módulos próprios.

1. **Entrada (processo pai).**
   - Rota nova, na fase 1 só para o consultor (`admin_required`), enviando em nome do lead. A rota pública fica para depois de resolver LGPD, anti-abuso e capacidade.
   - `raio_x_servico.processar_envio` ganha os parâmetros `executar=` e `origem=`. Mudam só as linhas 100 e 107; o resto fica igual.
   - Os nomes dos arquivos do lead não são gravados.
   - A transação de leitura é encerrada antes do processo isolado: nada fica travado durante os até 120 s.
2. **Processo isolado.**
   - Nova operação `avaliacao_cenario` em `etl_worker.py`, cópia do bloco do Raio-X (61-69).
   - `etl_runtime.run_avaliacao_cenario`, com prazo de 120 s e validação estrita do retorno (chaves exatas, colunas de mesmo comprimento, números finitos).
   - `validate`, `calculate` e `raio_x` não mudam.
3. **Motor (sem banco).**
   - **Refatoração sem mudança de saída.** `raio_x.diagnosticar` passa a ser a composição de duas funções:
     - `_ler_envio`, a leitura (linhas 399-411);
     - `_combinar`, a combinação (413-506).

     A prova é um golden do JSON de antes e de depois, mais os 62 testes.
   - **Módulo novo `avaliacao_motor.py`:**
     - regra do ano: o ano só existe quando início e fim caem no mesmo ano civil, entre 2000 e o ano corrente;
     - recusa de arquivo sem período, com período que cruza o ano ou sobreposto a outro;
     - linhas produto × ano em **formato colunar**;
     - metadados do envio: versões, período, contagens de conflito, qualidade e situação, que o Raio-X hoje descarta;
     - guardas de magnitude e de valores finitos.
4. **Staging ampliada**, que vem de B.
   - `brainstorming_dados_pdv` ganha:
     - chave do produto;
     - as somas que as regras usam: quantidade com custo, faturamento com custo, custo total, perda, vazamento de preço e recuperável;
     - linhas e meses;
     - as marcas `variacao`, `conflito_cadastral` e `estoque_negativo`;
     - a versão das regras;
     - unicidade por lead, ano e produto.
   - `lead_avaliacao` ganha:
     - a versão das regras, o período e o número de arquivos;
     - metadados só com contagens;
     - o consentimento versionado.
5. **Gravação e triagem (processo pai).**
   - `avaliacao_cenario.py` grava numa única transação aberta depois do processo isolado:
     - `FOR UPDATE` do lead e um SAVEPOINT;
     - DELETE e INSERT em lote, com um único `criado_em`;
     - o resumo do lead: índice e recuperável do mesmo `diagnosticar`; plano de `raio_x.recomendar`, no processo pai.
   - `triagem_cenario.py` é uma função pura e versionada sobre as colunas gravadas. Devolve os blocos Urgência, Atenção e Eletiva só com contagens e totais, sem nome de produto, porque o expurgo não apaga os resumos.
   - O comando `flask leads retriar <id>` refaz a triagem durante o TTL.
6. **Fica de fora:**
   - de B: a higienização no processo web, os lotes e a máquina de estados entre commits;
   - de A: o `modo=` dentro de `processar_arquivos_analise` e `_calcular_kpis_local`, e as marcas congeladas no processo isolado.

   Os lotes só entram se o ensaio de capacidade mostrar que o envio único não cabe e não houver como ampliar a capacidade.

**Esforço estimado do híbrido:** 11,5 a 18 dias de desenvolvimento, depois das decisões do dono. O risco de estouro está na especificação da triagem e na LGPD, não no motor.

### 4.4 O que nenhum desenho resolve

- **"Massa bruta" tem limite.** Significa até 36 relatórios de vendas por produto e período, num envio de até 20 MiB e num job de 120 s.
  - Um log de cupons de 3 anos não entra: as vendas não têm data por linha, e cada arquivo tem teto de 500 mil células.
  - Mudar isso exige mexer em `etl_inputs`, o leitor endurecido do SEC-005, e isso atinge também a produção.
- **Capacidade.** XLSX de 3 anos com 5.000 produtos passa do prazo, e o plano gratuito do Render tem 0,1 CPU. Antes de abrir para o público, é preciso um ensaio em Linux e uma decisão de capacidade: plano pago, worker dedicado ou limite documentado de entrada.
- **A triagem não existe.** O algoritmo de Urgência, Atenção e Eletiva não está em lugar nenhum: `models.py` (1332-1334) só tem as colunas.
  - A especificação da Avaliação de Cenário citada na migração (linha 15) não está no repositório.
  - No código, "triagem" é a triagem de segurança da Baia, e "semáforo" é o card da análise mensal (`insights.gerar_semaforos`, com crítico/atenção/oportunidade pela Pressão de Estoque). É o único classificador de **prioridade** em 3 níveis, e só seu formato de card é reaproveitável.
  - Há outros classificadores de 3 classes, que não medem urgência: a Curva ABC (`_classificar_curva_abc`), a recomendação BRONZE/PRATA/OURO (`raio_x.recomendar`) e as faixas de cor do índice de saúde, que existem só no template (`templates/onboarding/diagnostico.html` 52: verde a partir de 80, âmbar a partir de 50). Se a triagem do lead partir do índice, essas faixas devem virar constantes no código, para não nascer mais uma cópia.

### 4.5 Riscos de regressão e como evitá-los

1. **Raio-X dos clientes.** A refatoração de `diagnosticar` pode mudar a ordenação estável, o "último nome", a união dos conflitos e o corte da precificação. Mitigação:
   - golden do JSON gravado **antes** da refatoração;
   - os 62 testes;
   - `CHAVES_RESULTADO` e `VERSAO_REGRAS` sem mudança.
2. **Protocolo SEC-005.**
   - O ramo novo não pode afrouxar `validate` (um caminho), `calculate` (dois) nem o limite de tamanho de arquivo.
   - `avaliacao_motor.py` não pode importar `models`, `config` nem `flask`.
   - O processo isolado real só roda no CI POSIX; no Windows, os testes usam o emulador `tests/raio_x_support.executar_como_o_worker` (39-49).
3. **Parâmetro-bandeira.** Qualquer `if modo` na limpeza muda os três produtos de uma vez, e o golden cobre um comportamento só.
4. **Teto de saída.** Acima de cerca de 24.500 linhas produto × ano, o processo isolado devolve `output`, sem resultado parcial. O motor precisa recusar antes, com mensagem própria.
5. **Vaga única.** Uma avaliação ocupa por até 120 s a vaga de todos os clientes. Até existir fila ou capacidade, a entrada fica só com o consultor.
6. **Migração.**
   - Ajustar `f4c2a8e1d9b6` só é possível se nenhum banco persistente a recebeu. Caso contrário, entra uma revisão nova.
   - `tests/test_leads_avaliacao.py` (53-65) e o ensaio `tests/test_leads_postgresql.py` precisam acompanhar.
7. **Números.** O SQLite não impõe `NUMERIC(p,s)`: no ensaio, aceitou 10.000.000.000,00 em `NUMERIC(12,2)`. O estouro só aparece no PostgreSQL e derruba o SAVEPOINT. Por isso são necessárias guardas de magnitude no motor e um ensaio no PostgreSQL com valores no limite.
8. **Arredondamento.** `vazamento_total_r$` (a soma das linhas) difere do recuperável do Raio-X em centavos. Nenhum teste ou tela pode exigir igualdade estrita entre os dois.
9. **Ano e período.**
   - "99" vira 2099.
   - A data de emissão na mesma célula estende o período, e um arquivo legítimo pode parecer cruzar o ano. A mensagem de recusa precisa dizer o porquê.
10. **TTL e LGPD.**
    - Sem um `criado_em` único por lote, o expurgo divide o lead.
    - Um bloco de triagem com nome de produto escapa do expurgo.
    - Um lead criado antes do processo isolado que fica sem linhas nunca expira (migração, 108-113).

### 4.6 Próximos passos

1. **Decisões do dono, que bloqueiam o início do código:**
   - **Quem envia na fase 1.** Recomendação: o consultor.
   - **Especificação da triagem** Urgência, Atenção e Eletiva.
   - **Definição de cada marca e do vazamento.** As candidatas medidas são:
     - sem custo: faturamento maior que o faturamento com custo;
     - abaixo do custo: perda > 0;
     - vazamento: o recuperável;
     - omissão do ERP: falsa até existir uma regra.
   - **`valor_venda_bruto`.** Recomendação: a definição do Raio-X, que bate com `saude.faturamento_total`.
   - **Regra do ano.**
   - **LGPD do lead:**
     - base legal e aviso;
     - consentimento versionado;
     - retenção do `lead_avaliacao`;
     - destino dos leads convertidos.
   - **Agendamento do expurgo:** cron do Render ou `pg_cron`.
   - **Capacidade.**
2. **Ensaio de capacidade em Linux** com o processo isolado real: 36 CSV e 36 XLSX, com 2.000 e 5.000 produtos. Medir tempo, memória e bytes de saída.
3. **Golden do Raio-X:** gravar o JSON de `diagnosticar` antes de tocar no código.
4. **Refatoração de `diagnosticar`**, num commit isolado e sem mudar a saída.
5. **Motor novo**, com emulador no Windows, teste do processo real no CI e teste de que o módulo não carrega banco.
6. **Migração** (ajuste ou revisão nova). Testes em SQLite e ensaio no PostgreSQL 18 local: RLS, CHECKs, `NUMERIC` no limite, unicidade e expurgo concorrente.
7. **Gravação e triagem.** O ensaio de fidelidade do juiz vira teste automático.
8. **Interface do consultor.** A rota pública só depois de LGPD, anti-abuso e capacidade.
9. **Documentação:** criar `Docs/AVALIACAO_DE_CENARIO.md` e atualizar `Docs/BAIA_DE_SEGURANCA_E_LEADS.md`.
10. **Autorização:** commit, push, migração no Supabase e deploy só com autorização expressa do dono.

---

## 5. Lacunas, defeitos medidos e divergências

### 5.1 O que não existe hoje

**Leads e triagem**
- Gravador de `lead_avaliacao` ou `brainstorming_dados_pdv`: não há rota, formulário, serviço nem operação do processo isolado.
- Algoritmo de triagem Urgência/Atenção/Eletiva e a especificação da Avaliação de Cenário.
- Cálculo de `flag_omissao_erp`, `flag_venda_sem_custo` e `flag_venda_abaixo_custo`.
- Data por linha nas vendas e agregação por `ano_exercicio`.
- Unicidade e versionamento em `brainstorming_dados_pdv`: um reprocessamento duplicaria linhas.
- **LGPD do lead:** não há colunas de consentimento, base legal, versão do aviso nem IP. `AceiteDocumento` exige usuário (`models.py` 1101-1103), e não há retenção para o próprio `lead_avaliacao`.
- Agendador do expurgo de 30 dias: hoje só roda manualmente, e não alcança os backups.
- Rota anônima, conversão de lead em cliente e interface administrativa de leads.

**Entrada**
- Leitor de TXT e de XLS no portal.
- Decodificação real de UTF-16 e detecção de codificação.
- Aviso de abas ignoradas no XLSX.
- Tipo declarado na triagem da Baia: a estrutura mínima aceita o arquivo se ele parecer de vendas ou de compras, sem saber qual campo o usuário escolheu (seção 1.3, passo 7).

**Processamento**
- Leitura em blocos, fila, execução assíncrona e paralelismo.
- Saída do processo isolado por arquivo ou fluxo.
- Guarda de finitude no cálculo da análise mensal: uma quantidade infinita derruba a serialização com erro genérico.
- Aviso de mês faltante e detecção de períodos sobrepostos entre envios. O Raio-X só avisa sobreposição dentro do envio.
- Conferência do total contra o PDV no Raio-X.
- Persistência de alertas, telemetria e resumo da Curva ABC da análise mensal.

### 5.2 Defeitos de regra medidos (corrigir com teste antes de crescer o volume)

| # | Defeito | Onde | Efeito |
|---|---|---|---|
| 1 | Rodapés "Total do dia" e "Total do período" contam como produto | `_PADRAO_LIXO` (493-497) e `_NAO_PRODUTOS` (146-150) não os cobrem | Multiplicam o faturamento. Só a trava do PDV denuncia, quando há total digitado |
| 2 | O filtro de lixo apaga produtos reais | `_PADRAO_LIXO` (493-497) | "Filtro de Café", "Software", "Papel Impresso" e "Resumo" somem. Rodapés acentuados ("Página", "Usuário") passam como produtos de R$ 0,00 |
| 3 | Leitura dupla de VENDAS no `calculate` | `_calcular_kpis_local` (941) | Um XLSX de 450 mil células levou 67,8 s, acima dos 60 s do `calculate`, que não dobra o prazo |
| 4 | O alerta de custo não distingue custo unitário de custo total, e custo zero nunca é sinalizado | `_calcular_kpis_local` (1009-1022) | Subestima as vendas abaixo do custo |
| 5 | COMPRAS soma NF cancelada e descarta devoluções e bonificações | `carregar_compras` (702-722) | Total comprado e Pressão de Estoque distorcidos |
| 6 | Aliases de ERP faltando e falsos positivos por substring | `ALIAS_VENDAS` e `ALIAS_COMPRAS` (359-412) | Relatório recusado ou coluna errada |
| 7 | `_parse_valor` lê "2-" como +2 e "1/2" como 12 | `_parse_valor` (156-202) | Quantidade com sinal ou valor errado |
| 8 | Milhar en-US "12,500" lido como 12,5 sem alerta | `_converter_serie_monetaria` (308-322) | Valor mil vezes menor, em silêncio |
| 9 | O KPI "maior faturamento" é o 2º colocado quando coincide com o mais vendido | `_calcular_kpis_local` (921-935) | Não é o líder real de receita |
| 10 | Insights mostra "Giro de estoque saudável" com faturamento ≤ 0 | `insights.gerar_semaforos` (57) | Mensagem positiva indevida |
| 11 | A mensagem específica de dados é trocada por uma genérica no processo isolado | `etl_worker.main` (73-81) | Exemplo: "todas as linhas estão canceladas" não chega ao usuário |
| 12 | UTF-8 com um byte cp1252 perdido vira mojibake | `etl_inputs._csv_input` (148-172) | O mesmo produto se divide em duas chaves entre meses |
| 13 | XLSX com várias abas: só a maior é lida | `etl_inputs._xlsx_input` (551-582) | Meses perdidos sem aviso |

### 5.3 Duplicações e código morto (não multiplicar)

- **Devoluções no Raio-X.** O total é recalculado em `raio_x.py` 247, quando já está em `attrs['situacao']['valor_devolucoes']` (`etl_processor.py` 595-600).
- **Preço unitário.** É convertido duas vezes: `raio_x.py` 179-180 e `etl_processor.py` 651-655.
- **Dinheiro.** Cada valor é limpo duas vezes: `_converter_serie_monetaria` (310) e `_interpretar_monetario` (270).
- **Regex de milhar.** Há dois idênticos, em `etl_inputs.py` 30 e `security_gatekeeper.py` 610. Os dois precisam continuar iguais, porque a cópia da Baia depende da regra do leitor.
- **Cópia limitada.** Há duas quase iguais: `etl_uploads.py` 81-97 e `raio_x_servico.py` 47-66.
- **Assinatura.** `etl_inputs._csv_signature` (134-145) é um subconjunto de `identificar_conteudo` (190-214).
- **Código morto:**
  - `etl_processor.py` 44-45 (imports `csv` e `io` sem uso), 88-89 (`ENCODINGS` e `SEPARADORES` sem uso) e 328-339 (`_trim_vazios`);
  - `raio_x_servico.CAMPOS_FATIA` (39);
  - `admin._resolver_caminho` (963-966).
- **Formatação em reais.** Há quatro implementações: `raio_x._brl` (124-126), `ai_devolutiva.py` 85, `pdf_export.py` 167 e a macro Jinja `brl` (`templates/_macros.html` 15).
- **Limiares no template.** A tela do Raio-X repete o limiar de 10% de faturamento sem custo (`templates/onboarding/diagnostico.html` 62), que já existe em `raio_x.LIMIAR_SEM_CUSTO` (69), e guarda só no template as faixas de cor do índice (52).
- **Limite de histórico.** A regra "limite do nível atual ou, sem nível, o do menor nível" está escrita duas vezes: em `blueprints/onboarding.py` 70-71 e em `entitlements.limite_historico_meses`, que só os testes usam.

### 5.4 Documentação divergente do código

- **`etl_processor.py` 13** fala em 60 linhas de busca de cabeçalho; o código usa 100.
- **`Docs/RELATORIO_COBERTURA_ETL.md`** está desatualizado em três pontos:
  - as linhas 25-26 citam `_trim_vazios`, que é código morto;
  - a linha 31 cita o fallback `errors='ignore'`, removido em `2da719b`;
  - a linha 101 contradiz `Docs/BAIA_DE_SEGURANCA_E_LEADS.md` sobre UTF-16.
- **`Docs/RELATORIO_NEXO_SEC005_ETL_SEGURO.md`** dá 2 MiB como padrão de `max_output_bytes` na linha 73 (o teto de configuração, na mesma linha, já é 4 MiB) e limita o resultado a 2 MiB na linha 171. O padrão do código é 4 MiB desde `0ac01c8`.
- **Curva ABC só com produtos de faturamento positivo.** `nexobot/saber/indicadores.py` 63 e `pdf_export.py` 560-563 dizem isso, mas é falso desde `0ac01c8`.
- **Top 20 persistido.** `Docs/REESTRUTURACAO_COMERCIAL/00_01_BASELINE_GITHUB.md` (247, 411 e 657) fala nisso, mas foi corrigido em `0ac01c8`.

---

## 6. Histórico e preservação do legado

- **Início do histórico.** O git só tem o motor a partir de `fc63708` (19 de outubro de 2026). As regras anteriores não são visíveis no histórico. A referência mais antiga é o golden de equivalência capturado em `ff14879` (23 de outubro), em `tests/test_etl_financial_equivalence.py` e `tests/etl_equivalence_support.py`.
- **Inalterados desde `fc63708`:** `ALIAS_VENDAS`, `ALIAS_COMPRAS`, `_PADRAO_LIXO`, `_CABECALHOS_REPETIDOS` e `_NAO_PRODUTOS`.
- **Marcos de mudança:**
  - `2da719b` (24/10): isolamento SEC-005, SAVEPOINT, mensagens seguras e reescrita do leitor;
  - `bd8cace` (29/10): limite do nome;
  - `0ac01c8` (02/11): conversor monetário, cancelamentos e devoluções, e Curva ABC completa em centésimos (a regra do líder sempre em A já existia e foi mantida);
  - `bba0971` (02/11): trava do total do PDV;
  - `ec4ba95` (08/11): `_vendas_do_bruto` e cabeçalhos para o Raio-X;
  - `91e5fb0` (08/11): Baia de Segurança, neutralização e apóstrofo em `_parse_valor`.
- **Única regra antiga de leitura descartada.** Em `2da719b` saíram:
  - a leitura de `.xls` com `xlrd`;
  - a leitura de qualquer outra extensão como texto, com último recurso `errors='ignore'`.

  Hoje os dois casos são recusados pela extensão.
- **Regras de cálculo substituídas de propósito em `0ac01c8`.** A reescrita de `2da719b` manteve as regras de cálculo, e o golden foi capturado antes dela. Em `0ac01c8`, algumas mudaram de propósito:
  - **Dinheiro** (venda, custo e compras). Antes, era lido por `_parse_valor`, com ponto único decimal e sem sinal no fim: "12.500" valia 12,5 e "10,00-" valia +10 (medido com o `_parse_valor` de `0ac01c8~1`). Hoje, `_converter_serie_monetaria` lê "12.500" como 12.500,00 (ou 12,5 numa coluna com ponto decimal) e "10,00-" como −10.
  - **Curva ABC.** Antes, entravam só os produtos com faturamento positivo, os percentuais eram acumulados em ponto flutuante com teto de 100 (`cumsum().clip(upper=100.0)`) e só os 20 primeiros eram gravados (`TOP_N_CURVA_ABC = 20`). Hoje entram todos os produtos, inclusive zerados e negativos, em centésimos inteiros, e todos são gravados.
  - **Situação da venda.** Antes, as colunas de status eram ignoradas. Hoje as canceladas saem e as devoluções são deduzidas.
- **O golden foi revisado nesse commit.** Mudou o par `sales_derived_mixed_money` de `tests/fixtures/sec005_financial_expected.json`: a Curva ABC passou a incluir itens zerados e negativos, e entrou um alerta novo. Os KPIs desse par e os demais pares ficaram iguais (`tests/test_etl_financial_equivalence.py` 7-9). O arquivo ainda declara `source_commit` `ff148796`.
- **Consequência.** Análises processadas antes dessa mudança seguem a regra antiga e podem mudar ao reprocessar, se o relatório tiver valores como "12.500" ou "10,00-" em dinheiro, produtos com faturamento zero ou negativo, ou linhas canceladas.
- **Para não perder regras:**
  - toda mudança na leitura ou na limpeza deve passar pelo golden de equivalência e pelos testes de regras financeiras, do Raio-X e da Baia;
  - as regras novas do lead devem ficar em módulos próprios, depois da limpeza, nunca como variação dela.
