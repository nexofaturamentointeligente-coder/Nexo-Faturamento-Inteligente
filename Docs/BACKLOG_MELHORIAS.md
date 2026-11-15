# Backlog de melhorias futuras

Melhorias conhecidas que não bloqueiam entrega. Cada item traz a origem, o comportamento atual, o impacto e um
caminho de correção. A prioridade é decisão do dono.

## BM-01 · Nome de arquivo só com caracteres não latinos é recusado como formato não suportado

- **Registro:** 15/11/2026, na entrega do pré-cadastro e do Raio-X público (`Docs/PRE_CADASTRO_LANDING.md`), por decisão do dono. O comportamento já estava medido no inventário do ETL (`Docs/DIAGNOSTICO_MOTOR_ETL_INVENTARIO.md`, ENT-F23).
- **Comportamento atual:** o `secure_filename` do Werkzeug apaga os caracteres fora do ASCII antes de a extensão ser lida. Um nome sem nenhuma letra ou número ASCII antes do ponto perde a extensão: "продажи.csv" e "关系.csv" viram "csv", e "📊.xlsx" vira "xlsx". O arquivo é recusado com "formato não suportado", mesmo sendo um CSV ou XLSX válido. Nomes em português com acento passam: "relatório março.csv" vira "relatorio_marco.csv".
- **Onde:** em todo envio que tira a extensão do nome já passado pelo `secure_filename`:
  - `raio_x_servico.processar_envio`, usado pelo Raio-X do cliente, pela Avaliação de Cenário do consultor e pelo Raio-X público;
  - a pré-checagem `blueprints/avaliacao_publica._formato_recusado`;
  - o envio mensal em `blueprints/cliente.py`;
  - os envios de `blueprints/admin.py`.
- **Impacto:** baixo. Não há vazamento, perda de dado nem erro 500, e renomear o arquivo resolve. A mensagem, porém, fala do formato, não do nome. No Raio-X público, cada recusa ainda gasta uma das 3 tentativas por hora do IP.
- **Correção sugerida:**
  - tirar a extensão do nome original (`os.path.splitext(arquivo.filename)[1]`, sem espaços e em minúsculas) e conferi-la contra a lista aceita;
  - usar o `secure_filename` só para o nome exibido ou gravado;
  - pôr a regra numa função única, chamada por todos os envios, para elas não se separarem;
  - cobrir com testes de nomes em cirílico, chinês e emoji, e de nome só com sinais ("%.csv", "_.csv").
- **Prioridade:** baixa. Não bloqueou a entrega de 15/11/2026.
