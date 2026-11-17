# Microsserviço do Raio-X (captação de leads da landing)

Serviço à parte, dentro do monorepo, que recebe o pré-cadastro da landing da NEXO, faz a triagem dos relatórios enviados, guarda os aprovados no Storage do Supabase e grava o lead na tabela de leads da aplicação principal. Não importa nada da aplicação principal e tem dependências próprias (`requirements.txt`).

Decisões do dono em 15/11/2026:
- o lead vai para a tabela de leads atual, com uma migração aditiva na aplicação principal;
- e-mail e nome da empresa são obrigatórios;
- os interesses 1, 2 e 3 são as saídas da Etapa 3 do pré-cadastro, e só o 1 aceita relatórios.

Decisão do dono em 16/11/2026 (redesign da landing): o aceite do lead fica ligado à versão exata dos Termos de Uso e da Política de Privacidade publicadas no banco (`documento_legal`), com data, hora e IP, numa tabela própria (`aceite_documento_lead`, migração `c8e2a4f6b0d1`). A variável `TERMOS_VERSAO` deixa de ser usada.

## Arquivos

| Arquivo | Papel |
| :--- | :--- |
| `app.py` | Rotas, CORS, ordem das checagens e a gravação (Storage e banco) |
| `configuracao.py` | Variáveis de ambiente, a lista de problemas de configuração e os avisos da subida |
| `validacao.py` | Campos do formulário (nome, empresa, e-mail, WhatsApp, interesse, termos e os ids dos documentos lidos) e o IP do visitante para o aceite |
| `sanitizacao.py` | Triagem de cada relatório pelo conteúdo |
| `armazenamento.py` | Cliente do Storage do Supabase (biblioteca padrão, HTTPS, sem redirecionamento) |
| `repositorio.py` | INSERT do lead e do aceite de cada documento, teto diário e versões vigentes conferidos sob trava na mesma transação, conferência do lead gravado e das tabelas (psycopg2) |
| `limite.py` | Limite de envios por IP (IPv6 pela rede /64), em memória |
| `wsgi.py` | Entrada do gunicorn (nenhuma thread nasce na importação) |
| `gunicorn.conf.py` | Lido sozinho pelo gunicorn: formato do log de acesso sem dado pessoal e a conferência do banco e do bucket na subida do worker (`post_worker_init`) |
| `preparar_bucket.py` | Cria ou confere o bucket privado `leads-raio-x` |
| `instalar_dependencias.py` | Build verificado: pip fixado e conferido por SHA-256 antes de instalar os requisitos |
| `.python-version` | Python 3.12.10, o mesmo da aplicação principal |
| `tests/` | Testes (unittest), sem rede nem banco real |

## API

### `GET /ping`, `GET /health` e `GET /`

Keep-alive para o serviço não dormir: um agendador externo chama a cada 10 minutos (o Render Free dorme depois de 15 minutos sem tráfego). As três rotas são a mesma função e respondem só `{"status": "alive"}`, com HTTP 200, em JSON compacto: o corpo exato é `{"status":"alive"}` seguido de quebra de linha. No agendador, confira o HTTP 200 (ou a palavra-chave `alive`), e não o texto com espaço. Não tocam no banco nem no Storage, não passam pelo CORS nem pelo limite por IP e não executam regra de negócio. Aceitam só `GET` e `HEAD`; outro método, inclusive `OPTIONS`, recebe 405 com o cabeçalho `Allow` (GET e HEAD). Respondem mesmo com a configuração incompleta e com o banco ou o Storage fora do ar, enquanto houver thread livre no gunicorn (ver **Limites conhecidos**). O `/ping` é também o health check do Render. Antes de ligar o agendador, leia o aviso das horas do plano Free em **Implantação**.

### `POST /api/leads/raio-x` (multipart/form-data)

| Campo | Regra |
| :--- | :--- |
| `nome` | Obrigatório, até 120 caracteres. |
| `empresa` | Obrigatório, até 120 caracteres (vai para o nome da loja do lead). |
| `email` | Obrigatório, até 254 caracteres, guardado em minúsculas. |
| `whatsapp` | Obrigatório, do Brasil, com DDD (regra abaixo). Guardado só com DDD e número. |
| `interesse` | `1` (quer o Raio-X, com relatórios), `2` (quer ajuda com os relatórios) ou `3` (só contato). Gravado como `raio_x`, `ajuda_relatorios` ou `so_contato`. |
| `termos_aceitos` | Tem de vir marcado: `true`, `1`, `on` ou `sim`. Falso ou ausente: 400. |
| `documento_termos` | O `id` da versão dos Termos de Uso que a landing mostrou (`documento_legal.id_documento`, o `id` de `GET /documentos-legais/vigentes` da aplicação principal). Obrigatório: inteiro positivo, só dígitos, sem zero à esquerda. |
| `documento_privacidade` | O `id` da versão da Política de Privacidade que a landing mostrou. Obrigatório, com a mesma regra, e diferente do anterior. |
| `arquivos` | Um ou mais relatórios, **só no interesse 1** (e obrigatórios nele): até 12, até 15 MB somados. |
| `campo_isca` | Opcional, invisível na landing: preenchido, o envio é recusado (robô). |

O aceite dos termos:
- `termos_aceitos` desmarcado, ou um dos dois ids ausente ou que não é inteiro positivo: 400 no campo `termos_aceitos`, "Leia e aceite os Termos de Uso e a Política de Privacidade.";
- sem versão publicada de um dos dois documentos: 503, "Os termos ainda não estão disponíveis. Tente mais tarde." (nada é gravado);
- ids diferentes das versões vigentes (o admin publicou outra enquanto o visitante lia, ou um id forjado): 409 no campo `termos_aceitos`, "Os termos foram atualizados enquanto você lia. Leia a versão atual e confirme de novo." Nada é gravado, e os relatórios que este envio já guardou no Storage são apagados. A landing busca as vigentes de novo e pede o aceite outra vez;
- com os ids certos, o lead é gravado com uma linha por documento em `aceite_documento_lead`: a versão aceita, o instante (o `now()` do banco, o mesmo de `termos_aceitos_em` do lead) e o IP do visitante. O lead guarda também o resumo das versões em `termos_versao`, como `TU-v1.0 PP-v1.0` (até 40 caracteres).

O IP do aceite é o mesmo endereço que o limite por IP conta: o do visitante, tirado do `X-Forwarded-For` com `PROXIES_CONFIAVEIS` (ver Segurança). Ele é normalizado: o IPv4 mapeado em IPv6 vira o IPv4 e o IPv6 sai na forma curta; o que não é IP, ou é endereço com escopo (`fe80::1%eth0`), fica nulo. O IP nunca vai para o log.

O WhatsApp segue as regras do envio pela Evolution da aplicação principal, mais estritas no que é ambíguo, para o número nunca virar o de outra pessoa:
- com `+` ou `00`, só o DDI 55 seguido de DDD e número;
- sem eles, o 55 do começo não vira DDI (12 ou 13 dígitos são recusados);
- DDD em uso, fixo de 8 dígitos começando por 2 a 5 ou celular de 9 começando por 9;
- recusa ramal (grupo final de 1 a 3 dígitos), letra, dois números no campo e o número de exemplo (um só dígito repetido);
- caracteres invisíveis saem, traços tipográficos contam como hífen e o `0xx` conta como o zero de tronco.

Resposta `201`:

```json
{"status": "recebido", "protocolo": "ABCD1234", "interesse": "raio_x", "status_arquivo": "parcial",
 "arquivos": [{"nome": "vendas-jan.csv", "situacao": "aprovado"},
              {"nome": "vendas.xlsm", "situacao": "recusado", "motivo": "Arquivo com macros (.xlsm) não é aceito: ..."}]}
```

O protocolo são os 8 primeiros caracteres do id do lead: o consultor acha o lead por ele em `/admin/leads`. A `status_arquivo` vale:
- `sem_arquivo` nos interesses 2 e 3;
- `aprovado` com todos os relatórios aprovados;
- `parcial` com parte deles aprovada;
- `recusado` com nenhum aprovado.

O lead é gravado nos quatro casos: o contato vale mesmo sem relatório aproveitável.

Erros (sempre `{"erro": "...", "campo": "..."}`, com `campo` quando há):

| Código | Quando |
| :--- | :--- |
| 400 | Campo inválido, termos não aceitos ou ids dos documentos ausentes ou inválidos, campo isca, regra dos arquivos pelo interesse, mais de 12 arquivos |
| 403 | Origem fora da lista (CORS estrito), inclusive com `CORS_ORIGENS` vazia: a origem é conferida antes de tudo |
| 409 | O visitante leu uma versão dos termos que não é a vigente (na primeira conferência ou na gravação), no campo `termos_aceitos` |
| 413 | Mais de 15 MB, um campo de texto enorme ou partes demais no formulário (dezenas de arquivos): a mensagem cita os dois limites |
| 415 | Pedido que não é multipart/form-data |
| 429 | Limite por IP (com `Retry-After`, legível pela landing pelo `Access-Control-Expose-Headers`) ou teto diário do serviço |
| 503 | Configuração incompleta (com a origem liberada), sem versão publicada de um dos documentos, banco ou Storage fora do ar, fila da triagem cheia (na hora, com `Retry-After`) ou a vez na triagem que não chega dentro da espera |

### Ordem das checagens

1. Origem.
2. Limite por IP.
3. Configuração.
4. Formato do pedido.
5. Tamanho.
6. Campo isca.
7. Aceite dos termos: a caixa marcada e os ids dos dois documentos lidos.
8. Campos.
9. Regra dos arquivos pelo interesse.
10. Primeira conferência, antes do trabalho pesado, numa ida ao banco: o teto diário (429) e as versões vigentes dos termos (503 sem documento publicado, 409 com ids de outra versão). Nada foi guardado ainda.
11. Triagem de cada relatório: uma por vez no processo, em ordem de chegada, com prazo por envio (`PRAZO_DA_TRIAGEM`). Só os envios com relatório entram na fila; os dos interesses 2 e 3 passam direto. Até dois envios esperam a vez; o seguinte recebe 503 na hora, com `Retry-After`. A fila protege só a triagem: pedidos presos em outra fase podem ocupar as 8 threads (ver **Limites conhecidos**). A espera vai até duas vezes o prazo, mais 10 s (70 s no padrão); depois, 503, também com `Retry-After`.
12. Gravação dos relatórios aprovados no Storage.
13. Gravação do lead e do aceite de cada documento no banco, numa transação só:
    1. a trava consultiva (`pg_advisory_xact_lock`) e o teto diário conferido de novo: envios simultâneos não passam juntos do teto;
    2. `documento_legal` travada em `SHARE MODE` e as versões vigentes lidas de novo. A trava espera a publicação de um documento que esteja em andamento (e a versão nova é vista) e faz esperar a que chegar depois, até o COMMIT. Assim o aceite gravado é sempre o da versão vigente no instante do COMMIT. Ela só disputa com a publicação: a leitura dos documentos e o aceite do cliente do portal não esperam;
    3. com ids de outra versão (a publicada durante o envio), nada é gravado: 409, e os relatórios deste envio saem do Storage;
    4. o INSERT do lead e o de um aceite por documento, com o mesmo `now()`.

O log da aplicação nunca leva nome, e-mail, WhatsApp, IP, a configuração nem as credenciais. O problema de configuração diz a variável, sem o valor. O log de acesso do gunicorn, que o Render liga por padrão, sai no formato do `gunicorn.conf.py`: instante, método, caminho sem a query, status e duração, sem IP, User-Agent nem Referer. No caminho, tudo o que não é ASCII visível, mais aspas, barra invertida e `%`, vira `%XX` antes de chegar ao log: ninguém forja linhas nele nem falsifica o status e a duração de uma linha.

### O que acontece quando algo falha no meio

- **O Storage falha num relatório.** Os relatórios deste envio são apagados, inclusive o que estava a caminho: o Storage pode ter gravado e a resposta se perdido (prazo esgotado, 502 do gateway). Apagar um objeto que não existe não é erro. Mas o Storage só cria o registro do objeto no fim da gravação, então o que estava a caminho pode ainda não existir na hora do DELETE e aparecer depois. Por isso, quando ele não é achado para apagar, o caminho vai para o log ("Relatório a caminho não achado para apagar"), para a conferência. Resposta 503.
- **O teto do dia fecha entre a primeira conferência e a gravação.** Os relatórios saem e a resposta é 429.
- **O admin publica uma versão nova dos termos durante o envio.** A gravação vê a versão nova (ver o passo 13): nem o lead nem o aceite são gravados, os relatórios saem e a resposta é 409.
- **O banco não confirma a gravação.** O COMMIT pode ter sido aplicado e só a resposta ter se perdido. Por isso o serviço confere, numa conexão nova, se o lead ficou gravado:
  - se ficou, responde 201 e não apaga nada;
  - se não ficou, apaga os relatórios e responde 503;
  - se nem a conferência responde, não apaga nada e põe no log os caminhos dos relatórios, para a conferência à mão.
- **Qualquer outro erro inesperado no meio da gravação.** Os relatórios do envio são apagados antes do 500.
- **Um relatório que não sai do Storage.** Vai para o log como órfão, com o caminho.

O que ainda pode deixar órfão sem registro é o processo morto no meio do envio (deploy, falta de memória). Por isso a pendência da rotina de retenção também cobre os prefixos `AAAA/MM/<id>` sem lead no banco.

## Segurança

- **CORS estrito.** Só a origem exata da landing (`CORS_ORIGENS`), e o localhost quando `PERMITIR_LOCALHOST=1`. Sem credenciais, só POST. O servidor também recusa (403) o POST com outra origem ou sem o cabeçalho Origin. O CORS sozinho só protege navegadores: um cliente fora do navegador pode mandar o Origin da landing, e por isso valem os limites abaixo.
- **Limites.** Por IP: 5 envios por hora e 10 por dia, em memória, zerados quando o processo reinicia. O IPv6 conta pela rede /64: trocar só o fim do endereço não zera o limite. O IP é o do visitante, tirado do `X-Forwarded-For` contando de trás para a frente as entradas dos proxies confiáveis (`PROXIES_CONFIAVEIS`). No Render, segundo medições publicadas por quem usa o serviço e a resposta da equipe do Render num pedido público, o cabeçalho chega como "visitante, borda do Cloudflare", e o padrão 2 acha o visitante. Com 1, todos os visitantes que saem pela mesma borda dividiriam o mesmo limite. O log confirma a contagem no serviço real (ver Implantação). Teto global de 100 leads por dia de Brasília, contado no banco e exato (trava consultiva na gravação).
- **Triagem dos relatórios** (`sanitizacao.py`). A extensão sai do nome original e só `.csv` e `.xlsx` passam. O tipo declarado pelo navegador tem de ser compatível com ela. Quem decide é o conteúdo.
- **`.xlsx`: lista branca.** O pacote é lido com o expat em blocos de 64 KB, sem expressão regular sobre o XML cru. A memória do expat não cresce com o tamanho da parte, e sim com o aninhamento, com os nomes distintos e com uma marcação ainda incompleta. Por isso, a cada bloco, a parte é recusada com mais de 256 elementos abertos ao mesmo tempo, mais de 2.000 nomes distintos (ou 256 KB de nomes, somados), ou mais de 1 MB numa só marcação ou comentário. Uma aba legítima fica muito abaixo disso; uma feita para estourar a memória cai cedo, com poucos MB.

  O tipo de cada parte vem do `[Content_Types].xml`, lido como o Excel o lê: só o `Default` e o `Override` do namespace dos tipos de conteúdo, e a parte ou a extensão repetida recusa o pacote. Além disso, o tipo tem de ser o do papel que a relação dá à parte: uma aba declarada como imagem ficaria sem leitura aqui e seria aberta como aba pelo Excel. O alvo de cada relação é resolvido como no OPC (o `%XX` decodificado, a barra invertida lida como `/`, sem o `#` e o `?`, e com o `.` e o `..` resolvidos), e a relação que não cai numa parte do pacote recusa a planilha: é onde a leitura daqui e a do Excel poderiam divergir. Passam:
  - partes de dados: pasta de trabalho, abas, textos, estilos, tema, tabelas, desenhos, gráficos, imagens e propriedades, comparadas pelo tipo de conteúdo exato;
  - relações de dados, todas internas.

  É recusado:
  - qualquer outro tipo de parte, em qualquer caminho: macro, ActiveX, objeto, vínculo, conexão, modelo;
  - relação externa;
  - DOCTYPE ou ENTITY em qualquer ponto da parte;
  - elemento de objeto, controle, DDE ou publicação na web (`webPublishItem`, `webPublishObject`), em qualquer namespace;
  - forma de desenho com macro atribuída (num `.xlsx`, a macro só pode estar noutro arquivo);
  - fórmula que chame endereço externo, programa ou DDE, ou aponte para outro arquivo. Entram aí:
    - as funções de busca, inclusive as novas de importação (`IMPORTTEXT`, `IMPORTCSV`);
    - a função passada como valor, que o Excel grava como `_xleta.WEBSERVICE` num `MAP` ou `LET`, e a função entre parênteses chamada em seguida, como `(WEBSERVICE)(A1)`;
    - a referência a outra pasta de trabalho, com caminho, endereço ou colchetes (`'\\host\pasta\[x.xlsx]Aba'!A1`).

    A fórmula é conferida pelo texto já decodificado (entidades, CDATA, UTF-16, prefixo de namespace e o escape `_xHHHH_` do OOXML) em células, nomes definidos, formatação condicional (inclusive o valor `val` do `cfvo`), validação, tabelas, gráficos e no texto de forma ligado a célula (`textlink`);
  - nome de parte com barra no começo, barra invertida, dois-pontos, segmento `.` ou `..` ou caractere de controle ou invisível, cru ou decodificado;
  - parte cifrada, compressão de bomba, mais de 2.000 partes, mais de 10.000 entradas no `[Content_Types].xml` ou num arquivo de relações, ou mais de 100 MB descompactados.

  Recusas conhecidas de planilhas legítimas (o visitante recebe o motivo e pode exportar como `.csv`):
  - comentários (usam VML);
  - tabelas dinâmicas;
  - rótulos de confidencialidade e partes `customXml`;
  - hiperlinks para fora (o link para outra aba do próprio arquivo passa, inclusive como o openpyxl o grava, com o alvo `#Aba!A1`);
  - formas com macro atribuída, comuns em planilha que já foi `.xlsm`;
  - o formato Strict do Office.
- **`.csv`:** precisa ser texto, sem byte nulo nem controle, e não um binário, página ou XML renomeado, com separador de colunas. É recusado com célula que comece como fórmula (`=`, `+`, `-`, `@`) e chame endereço externo, programa ou DDE, ou aponte para outro arquivo (injeção de fórmula ao abrir no Excel). Valem as mesmas funções do `.xlsx` e também os nomes delas no Excel em português (`HIPERLINK`, `SERVIÇOWEB`, `IMAGEM`, `FILTROXML`, `IMPORTARTEXTO`, `IDENT.REGISTRO`). O texto é decodificado como o Excel o lê: com a marca do UTF-8 (BOM), como UTF-8, mesmo com um byte inválido mais adiante; sem ela, UTF-8 ou cp1252. As células são lidas de todos os jeitos que o Excel poderia lê-las:
  - por cada separador (vírgula, ponto e vírgula, tabulação, barra vertical e o da linha `sep=`);
  - pela linha inteira;
  - com o tratamento de aspas, que junta `"=HYPER"LINK(` numa célula só;
  - com o fim de linha só com CR.

  Para não ler o arquivo inteiro célula a célula, as linhas candidatas são achadas antes, no texto sem aspas e com as letras ASCII em minúsculas, tirado do texto já decodificado: uma expressão curta por nome de função, buscada em janelas de 1 MB, e busca literal para o DDE, o `_xleta.`, o apóstrofo e o colchete. Só essas linhas são lidas célula a célula. A célula da leitura com aspas também é conferida sem as aspas que sobraram nela, como o texto da busca. O nome de função passado como argumento (`MAP(...;WEBSERVICE)`) só conta depois de um parêntese de chamada, colado a um nome ou a um `)`, ou depois de `MAP`, `LET`, `LAMBDA` e das outras funções que recebem função: uma coluna "Imagem" numa linha que começa com `-` passa, e `Brinde, @loja (imagem)` também. A função entre parênteses e chamada em seguida, como `=(WEBSERVICE)(A1)`, conta por cautela, porque não deu para conferir se o Excel a aceita. Uma linha candidata, ou um registro entre aspas, com mais de 1 MB recusa o arquivo como malformado: seria lido inteiro na memória.

  Recusas conhecidas de arquivos legítimos (o lead é gravado do mesmo jeito, com o motivo). Valem para uma linha, ou um trecho depois de `, -`, `| -` ou `, @`, que começa com `-`, `+`, `=` ou `@`:
  - com `|` seguido de `!` em qualquer coluna, como `-15,90;Estorno | cliente desistiu!`;
  - com o nome de uma função perigosa seguido de parêntese, como `- Imagem (frente)`, ou entre parênteses colados a outro, como `-1;Kit (Imagem)(2 un);-5,00`;
  - com o nome de uma função perigosa entre separadores depois de uma chamada na mesma linha, como `-1;TV(HD);Imagem;-899,00` (sem a chamada antes, `-1;Imagem;TV(HD);-899,00` passa);
  - com um texto entre apóstrofos que tenha barra seguido de `!`, como `-5,00;Promo 'leve 3/pague 2'!` (o caminho de outro arquivo);
  - com um colchete seguido de uma palavra e de `!`, como `-5,00;[Oferta]Imperdivel!` (a referência `[x.xlsx]Aba!`; com espaço depois do colchete, passa).

  Lida pela linha inteira, como o Excel leria com a tabulação, ou por outro separador, a linha seria uma fórmula. Afrouxar essa leitura abriria um caminho para o DDE num arquivo com `sep=`.
- **Macro:** `.xlsm`, `.xltm`, `.xlam` e tipos macroEnabled são recusados explicitamente.
- **Custo da triagem.** Uma triagem por vez no processo, com prazo por envio (padrão 30 s). O relatório que não termina a tempo é recusado com "O arquivo é grande demais para conferir agora". O lead é gravado do mesmo jeito. Medido num computador de mesa em 16/11/2026 (tempo; maior trecho sem soltar o GIL, que é quanto o processo inteiro, inclusive o `/ping`, fica parado; memória acima da base):

  | Caso | Tempo | GIL | Memória |
  | :--- | :--- | :--- | :--- |
  | Aba de 95 MB descompactados (um `.xlsx` de 10 MB) | 9,1 a 9,4 s (uns 10% a mais que sem as travas de estrutura) | 0,01 s | 1 MB |
  | `.xlsx` feito para estourar a memória do expat (aninhamento, nomes, marcação enorme) | recusado em 0,05 s | 0,01 s | até 4 MB |
  | `.csv` de 15 MB comum, só ASCII | 0,3 s | 0,02 s | 31 MB |
  | `.csv` de 15 MB com a coluna "Imagem" (nome de função) em toda linha | 1,2 a 3,1 s | 0,04 s | 28 MB |
  | `.csv` de 15 MB em cp1252, com acento | 0,4 s | 0,04 s | 32 MB |
  | `.csv` de 15 MB com caracteres fora do Latin-1 | 0,5 s | 0,02 s | 47 MB |
  | `.csv` de 15 MB com um caractere fora do plano básico (um emoji), com BOM e bytes inválidos | 0,6 a 0,9 s | até 0,25 s | 79 a 120 MB |
  | `.csv` de 15 MB feito para custar caro (linhas de 1 MB, milhões de gatilhos, colchetes ou bytes que o cp1252 não define) | 11 s ou mais, e recusado quando passa do prazo | até 0,27 s | até 90 MB |

  O plano Free do Render tem 0,1 de CPU, umas dez vezes mais lento. Nele, com o prazo padrão, cabem planilhas de uns 3 MB; acima disso, a recusa por prazo é provável. O Starter (0,5 de CPU) cobre o teto de 15 MB. O maior trecho medido sem soltar o GIL foi de 0,27 s (a conferência de uma linha candidata de 1 MB); no Free, uns 3 s, abaixo dos 5 s em que o health check do Render espera a resposta e dos 15 s seguidos de falha que tiram a instância do tráfego. Nenhuma expressão regular percorre o arquivo inteiro: as expressões da busca das linhas candidatas andam em janelas de 1 MB, e o prazo é conferido por janela, por linha, por separador e por registro.
- **Memória.** O `.xlsx` é lido em blocos de 64 KB, com as travas de estrutura acima. O pior caso medido do `.csv` fica perto de 120 MB na triagem, somados aos relatórios do envio, e por isso só há uma triagem por vez.
- **Armazenamento.** O caminho no Storage não tem nada que venha do visitante: `AAAA/MM/<id do lead>/<ordem>-<início do SHA-256>.<csv|xlsx>`. O lead guarda, em `arquivos_storage`, um JSON com o caminho, o nome exibido, o tamanho, o formato e o SHA-256 de cada relatório aprovado. O nome exibido sai sem pasta, controle, `< > : " / \ | ? *` e sem começar como fórmula. Quem o mostrar numa página ou exportar para planilha ainda tem de escapá-lo.
- **Credenciais.** Vêm só do ambiente. A `SUPABASE_KEY` é a chave secreta do projeto (`sb_secret_...`) e fica no servidor: nunca vai para a landing nem para o navegador. O Supabase anuncia a descontinuação das chaves legadas (`anon` e `service_role`) até o fim de 2026. A chave secreta nova vai só no cabeçalho `apikey`, como o Supabase pede; a service_role legada ainda funciona e vai também no `Authorization`. A chave publicável, a nova (`sb_publishable_...`) ou a `anon` legada (um JWT com `role` `anon`), é recusada na configuração.

## Configuração (variáveis de ambiente)

| Variável | Obrigatória | O que é |
| :--- | :--- | :--- |
| `DATABASE_URL` | sim | PostgreSQL do Supabase (o mesmo banco da aplicação principal), lida como a aplicação principal a lê: primeiro como na `_limpar_url_pg` do `config.py` (tabulação e quebra de linha saem de qualquer ponto, e a consulta é refeita, de modo que um `@` num valor dela não corta a URL noutro lugar), depois como no SQLAlchemy (a senha vai até o `@`, com `/` e `%` sem codificar, a porta é lida como número, e o `+` e o `%20` da consulta viram espaço). Os parâmetros `pgbouncer`, `connection_limit`, `pool_timeout` e `schema` saem, e `postgresql+psycopg2://` e `postgres://` valem. Host ou porta na consulta (o "vários hosts" do SQLAlchemy) e parâmetro repetido são recusados: o serviço poderia conectar noutro lugar. SSL exigido quando a URL não diz outra coisa. **No Render, use a URL do pooler, a mesma da aplicação principal**: a conexão direta (`db.<projeto>.supabase.co`) não conecta a partir do Render, que não sai por IPv6, salvo com o complemento de IPv4 do Supabase (o mesmo aviso de `Docs/PREPARACAO_RENDER.md`). URL inválida vira problema de configuração, sem derrubar o `/ping`. |
| `SUPABASE_KEY` | sim | Chave secreta do projeto (`sb_secret_...`), para gravar no bucket privado. A service_role legada também serve, até o Supabase desativá-la; a publicável (nova ou a `anon` legada) é recusada. |
| `SUPABASE_URL` | não | `https://<projeto>.supabase.co`. Sem ela, sai do `DATABASE_URL`: da conexão direta (`db.<projeto>.supabase.co`) ou do usuário `<papel>.<projeto>` do pooler, de qualquer papel. |
| `CORS_ORIGENS` | sim, em produção | Origens da landing, separadas por vírgula (`https://host`, sem caminho). **Fica vazia até a LGPD estar resolvida** (pendências): sem ela, todo POST e todo preflight recebem 403 (a origem é conferida antes de tudo), e o `/ping` continua no ar. Uma entrada inválida vai para o log pela posição na lista, sem o valor. |
| `PERMITIR_LOCALHOST` | não | `1` aceita `http://localhost` e `http://127.0.0.1` (só para testes). |
| `TERMOS_VERSAO` | não (ignorada) | Não é mais usada: a versão aceita vem dos documentos publicados no banco, lida na gravação do lead. Se estiver no ambiente, a subida deixa um aviso no log (sem o valor); pode tirá-la. |
| `SUPABASE_BUCKET` | não | Padrão `leads-raio-x`. |
| `LIMITE_POR_HORA`, `LIMITE_POR_DIA`, `TETO_DIARIO` | não | Padrão 5, 10 e 100. Números inteiros, só com dígitos ASCII. |
| `PRAZO_DA_TRIAGEM` | não | Segundos de triagem por envio, de 1 a 300. Padrão 30. A espera pela vez acompanha: duas vezes o prazo, mais 10 s. |
| `PROXIES_CONFIAVEIS` | não | Quantas entradas do `X-Forwarded-For` são de proxies confiáveis, de 0 a 5. Padrão 2: no Render, a da borda do Cloudflare e a do proxy do Render. Use 0 sem proxy na frente: senão o IP do limite pode ser forjado. **Conferir no serviço real antes de abrir a origem** (ver Implantação). |

Com algo faltando, o `/ping` continua respondendo, o envio responde 503 (403 sem `CORS_ORIGENS`) e cada problema aparece no log na subida, sem o valor.

## Banco: a migração `f2b6d8a4c1e9`

O lead vai para `lead_avaliacao`, com cinco colunas novas da migração aditiva `f2b6d8a4c1e9` da aplicação principal (`migrations/versions/`). Todas são anuláveis e têm CHECK:

| Coluna | Conteúdo |
| :--- | :--- |
| `interesse` | `raio_x`, `ajuda_relatorios` ou `so_contato` |
| `status_arquivo` | `sem_arquivo`, `aprovado`, `parcial` ou `recusado` |
| `arquivos_storage` | O JSON dos relatórios no bucket |
| `termos_aceitos_em` | O instante do aceite dos termos da landing (o mesmo de `aceite_documento_lead.aceito_em`) |
| `termos_versao` | O resumo das versões aceitas, como `TU-v1.0 PP-v1.0` (a prova é `aceite_documento_lead`) |

O microsserviço grava origem `site` e status `pendente`. O aceite dos termos fica em colunas próprias. Assim, quando o consultor processa os relatórios pela tela do lead (o reenvio, que reescreve `consentimento_em` e `versao_autorizacao`), o registro do aceite do visitante não se perde. `consentimento_em` e `versao_autorizacao` ficam nulos no lead do microsserviço. Os dados da avaliação ficam nulos até o consultor processar.

A aplicação principal funciona com ou sem a migração, porque as cinco colunas ficam fora do mapeamento do ORM (`exclude_properties` no modelo). Elas estão só na tabela, para o Alembic e o `create_all`, e nenhum INSERT ou SELECT do ORM as cita. O downgrade recusa enquanto houver lead com qualquer uma delas preenchida.

## Banco: a migração `c8e2a4f6b0d1` (o aceite dos termos do lead)

Tabela nova `aceite_documento_lead`, da migração aditiva `c8e2a4f6b0d1` (depois da `f2b6d8a4c1e9`), com o modelo `AceiteDocumentoLead` no `models.py`. É a prova do aceite do lead, como `aceite_documento` é a do cliente do portal (aquela não serve ao lead: o usuário é obrigatório nela).

| Coluna | Conteúdo |
| :--- | :--- |
| `id_aceite` | Chave |
| `id_lead` | O lead (`lead_avaliacao.id`), com `ON DELETE CASCADE`: o aceite sai com o lead |
| `id_documento` | A versão aceita (`documento_legal.id_documento`), sem cascata: a versão aceita não pode ser apagada |
| `aceito_em` | O instante do aceite (o `now()` do banco, o mesmo do lead) |
| `ip` | O IP do visitante, normalizado; nulo quando ausente ou inválido. Até 45 caracteres |

Uma linha por documento e lead (`UNIQUE(id_lead, id_documento)`), com índice em `id_documento`. No PostgreSQL, RLS ligado, com as políticas `deny_api_access` (restritiva, para `anon` e `authenticated`) e `acesso_servidor` (para `postgres` e `service_role`), como em `lead_avaliacao`; os privilégios de `anon` e `authenticated` na tabela e na sequência da chave são revogados. O downgrade recusa enquanto houver aceite gravado.

A aplicação principal só lê a tabela, no bloco "Aceite dos termos" da página do lead (`/admin/leads/<id>`): os documentos, as versões, a data e a hora (horário de Brasília) e o IP. Sem a tabela, a página sai sem o bloco.

**Ordem da implantação: as migrações antes do deploy do serviço.** O deploy do Render não migra sozinho, e o serviço sem a tabela responde 503 a todo envio (nada é gravado). No banco da aplicação principal, com backup antes, como as anteriores:

```
flask db upgrade f2b6d8a4c1e9:c8e2a4f6b0d1 --sql   (prévia, num banco já na f2b6d8a4c1e9)
flask db upgrade c8e2a4f6b0d1
```

Num banco ainda na `e7a1c3f5b9d2`, a prévia é `e7a1c3f5b9d2:c8e2a4f6b0d1`, e o upgrade aplica as duas. Depois, nesta ordem:
1. publicar os Termos de Uso e a Política de Privacidade (Configurações › Documentos Legais da aplicação principal). Sem as duas versões publicadas, o envio responde 503;
2. implantar esta versão do serviço e conferir a conferência da subida no log (ver Implantação);
3. ligar a landing, que manda os ids das versões que mostrou.

## Storage

Rode `python preparar_bucket.py` uma vez, com `SUPABASE_URL` e `SUPABASE_KEY` no ambiente. O `DATABASE_URL` só é preciso sem o `SUPABASE_URL`, para tirá-lo dele. O roteiro cria o bucket `leads-raio-x`:
- privado;
- com teto de 15 MB por arquivo;
- só com os tipos de `.csv` e `.xlsx`.

Se o bucket já existe, o roteiro só confere que é privado. O acesso aos arquivos é só pela chave secreta do servidor. Nenhuma política pública deve ser criada para o bucket.

O backup do banco (`pg_dump`) não leva os objetos do Storage, só os caminhos guardados no lead. Antes de o serviço receber dados, o roteiro de backup da aplicação principal precisa passar a cobrir o bucket (pendências).

## Implantação (Render, serviço web separado)

- **Diretório raiz:** `microservico_raiox`
- **Python:** 3.12.10, pelo `.python-version` da pasta (ou `PYTHON_VERSION=3.12.10` nas variáveis). O `psycopg2-binary` 2.9.9 não tem wheel para o 3.13.
- **Build:** `python -I instalar_dependencias.py`. É a mesma proteção da SEC-006B da aplicação principal (`tools/install_dependencies.py`): mesmo pip, mesmo SHA-256 e as mesmas variáveis de ambiente repassadas. O pip da imagem não processa os requisitos. O pip 26.2.2 é baixado de files.pythonhosted.org e conferido pelo SHA-256. A versão é confirmada pelos metadados e pelo módulo importado, e só então ele instala o `requirements.txt`, sem cache e sem configuração herdada.
- **Start:** `gunicorn -w 1 --threads 8 --timeout 60 -b 0.0.0.0:$PORT wsgi:app`
- **`gunicorn.conf.py`:** o gunicorn o procura na pasta em que o comando começa, antes de aplicar qualquer `--chdir`, e essa pasta tem de ser a do serviço (o diretório raiz `microservico_raiox`). Um `-c`, no start ou no `GUNICORN_CMD_ARGS`, faz ele ler outro arquivo no lugar deste. Em qualquer dos dois desvios, o log de acesso volta ao formato padrão, com IP, e a conferência da subida não roda. Ele fixa o formato do log de acesso sem dado pessoal e começa a conferência da subida no worker, depois do fork. O Render põe por padrão `GUNICORN_CMD_ARGS="--preload --access-logfile - --bind=0.0.0.0:10000"` nos serviços Python: o log de acesso fica ligado, o app é importado no processo mestre (por isso nenhuma thread nasce na importação) e o `-b` do start vence o `--bind`. No primeiro deploy, confira que as linhas de acesso saem sem IP.
- **Health check:** `/ping`. Ele não toca no banco nem no Storage, mas usa uma das 8 threads do gunicorn como qualquer pedido (ver **Limites conhecidos**).
- **Variáveis:** as da tabela acima.

Um worker só: o limite por IP fica em memória.

**Conferências no log, antes de abrir a origem.** Nenhuma delas grava dado pessoal.
- Na subida do worker, numa thread à parte (gancho `post_worker_init` do `gunicorn.conf.py`, depois do fork), o serviço lê as tabelas pelas colunas das migrações (a de leads, a do aceite e as versões vigentes de `documento_legal`), trava `documento_legal` como a gravação trava e lê o bucket. O log mostra "Conferência da subida: banco ok, com as tabelas das migrações f2b6d8a4c1e9 e c8e2a4f6b0d1." e "Conferência da subida: bucket ok, privado." Senão, mostra o tipo do erro: por exemplo, `OperationalError` para a URL direta no Render, `UndefinedColumn` ou `UndefinedTable` sem as migrações, `InsufficientPrivilege` para um papel que não pode travar `documento_legal` e 401 ou 403 para uma chave recusada. Sem versão publicada de um dos documentos, o log avisa: "Conferência da subida: documento sem versão publicada (...); os envios respondem 503 até a publicação." Com `TERMOS_VERSAO` no ambiente, avisa que ela é ignorada.
- Na primeira vez que aparece cada contagem de entradas do `X-Forwarded-For` (de 0 a 6, e "7 ou mais"), o log mostra só a contagem: "X-Forwarded-For com N entrada(s); PROXIES_CONFIAVEIS=2." Com o serviço no ar, abra `https://<serviço>/ping` no navegador, de fora, e procure a linha do mesmo instante (cada linha tem a hora). Se ela disser 2, o padrão está certo. Se disser outro número, use esse número em `PROXIES_CONFIAVEIS`. Se nenhuma linha nova aparecer, a sua contagem já tinha aparecido antes. A contagem 0 é a do health check interno do Render.

**Limites conhecidos (threads do gunicorn).**
- As 8 threads atendem tudo, inclusive o `/ping`. Oito pedidos presos ao mesmo tempo deixam o `/ping` sem resposta até um deles soltar, e o health check do Render pode tirar a instância do tráfego. Prendem a thread: o banco ou o Storage lentos, o corpo do envio chegando devagar e o corpo que o app não leu, de qualquer pedido (qualquer método e rota, inclusive o `/ping`: 403, 404, 405, 413, 415, 429, 503 e até o 200 de um GET com corpo), que o gunicorn descarta depois da resposta, em keep-alive, sem prazo. A fila da triagem limita só a triagem.
- Isso só vale se o proxy do Render (e o Cloudflare na frente dele) repassar o corpo em fluxo. Antes de abrir a origem, meça de fora: 6 a 8 uploads com `curl --limit-rate 1k` enquanto chama o `/ping`.
- Se o corpo chegar em fluxo, a defesa que cobre o descarte é fechar no gunicorn a conexão de todo pedido com corpo (`Content-Length` ou `Transfer-Encoding`), de qualquer método, num `pre_request` com `req.force_close()`, ou `keepalive = 0`; o custo é que a resposta antecipada (413, 429, 503) pode virar um erro de conexão na landing. Prazo para a leitura do corpo e teto de envios simultâneos por IP cobrem só o corpo que o app está lendo, não o descarte. Um teto global de envios sem teto por IP foi testado e descartado: cinco conexões lentas de um só IP bastavam para recusar todos os envios, sem nenhum alerta, porque o `/ping` seguia respondendo.

**Plano e keep-alive.**
- O plano Free do Render dá 750 horas por mês por workspace, somando todos os serviços Free. Um serviço acordado o mês inteiro gasta de 720 a 744 horas.
- Com a aplicação principal no Free e acordada (ela tem o próprio despertador `/ping`), um segundo serviço Free acordado estoura as horas por volta do dia 15. Aí o Render suspende todos os serviços Free do workspace, inclusive a aplicação principal, até o mês seguinte.
- Por isso: o microsserviço no plano Starter (não dorme, dispensa o agendador e tem 0,5 de CPU para a triagem), ou a aplicação principal num plano pago. Agendador externo chamando `GET /ping` (ou `GET /health`) a cada 10 minutos só com as horas conferidas.
- O `/ping` não consulta o banco, de propósito. Com o Supabase no plano Free, o projeto pausa depois de 7 dias sem consultas: mantenha no monitor também o `/healthz/db` (`SELECT 1`) da aplicação principal, como em `Docs/WHATSAPP_EVOLUTION.md`. Com o projeto pausado, o `/ping` continua respondendo `alive`, mas os envios recebem 503.

## Testes

Na pasta do microsserviço:

```
python -m unittest discover -s tests -t .
```

Os testes não usam rede, banco nem Storage reais. O banco e o Storage entram como dublês, o cliente do Storage é testado contra um servidor HTTP local, e o repositório, com uma conexão espiã: a trava consultiva tem de vir antes da contagem, e a trava de `documento_legal` e a leitura das versões vigentes antes do INSERT, na mesma transação. As regras do aceite têm testes próprios: os ids ausentes ou inválidos (400), sem documento publicado (503), os ids de outra versão na primeira conferência (409, sem nada no Storage) e na gravação (409, com os relatórios apagados), o IP do visitante pelo `X-Forwarded-For` e o IP fora do log. O instalador é testado com o download e os processos simulados.

O ensaio da gravação num PostgreSQL de verdade, com as migrações aplicadas, é a etapa 6d de `tests/test_leads_postgresql.py`, na aplicação principal. Ela cobre o lead gravado, a conferência do lead e as CHECKs. Cobre também o teto exato com oito envios simultâneos, com a janela entre a contagem e o INSERT alargada: sem a trava, os oito passariam juntos. E o aceite: os documentos publicados pelo caminho do admin; dois aceites com o IP e o instante do lead; sem documento publicado, nada gravado; ids antigos, trocados ou forjados, nada gravado; a publicação em andamento, que a gravação espera e cuja versão nova ela recusa; a publicação que chega durante a gravação, que espera o COMMIT do lead; as restrições, o RLS, as políticas e os privilégios da tabela e da sequência; e o downgrade recusado com aceite, aceito sem ele e o novo upgrade. Sem a trava de `documento_legal`, ou com ela solta antes do COMMIT, o ensaio falha (as duas mutações foram conferidas em 16/11/2026).

## Pendências antes de ligar na landing

Cada passo depende de autorização do dono.

1. **LGPD**, antes de qualquer dado entrar:
   - os Termos de Uso e a Política de Privacidade publicados em Configurações › Documentos Legais (a versão 1.0 virá dos textos do dono); a versão aceita vai para `aceite_documento_lead`, e `TERMOS_VERSAO` não é mais usada;
   - base legal;
   - prazo de guarda da prova do aceite: hoje ela sai com o lead (`ON DELETE CASCADE`), como a do cliente sai com o usuário;
   - prazo de retenção dos relatórios no bucket, com uma rotina que apague os vencidos e os prefixos `AAAA/MM/<id>` sem lead no banco (hoje eles ficam até alguém apagar);
   - atendimento a pedido de exclusão (o lead, os aceites e os arquivos dele).
2. Aplicar as migrações `f2b6d8a4c1e9` e `c8e2a4f6b0d1` no banco de produção, com backup, **antes** de implantar esta versão do serviço (ver "Banco: a migração `c8e2a4f6b0d1`").
3. Criar o bucket com `preparar_bucket.py`, com a chave secreta (`sb_secret_...`).
4. Implantar o serviço (plano conforme o aviso das horas) com `CORS_ORIGENS` vazia. Conferir no log a conferência da subida e a contagem do `X-Forwarded-For`, e ajustar `PROXIES_CONFIAVEIS` se preciso (ver Implantação). O IP gravado no aceite é o que essa contagem acha: com o valor errado, ele vira o da borda do Cloudflare, e não serve de prova. Preencher a origem só depois do passo 1.
5. Ligar a landing a este endpoint (hoje o pré-cadastro só monta a mensagem do WhatsApp). Na aplicação principal, que serve a landing:
   - a CSP de `seguranca_http.py` precisa liberar a origem exata do microsserviço em `connect-src` (envio por `fetch`) ou em `form-action` (envio por formulário);
   - hoje ela só permite `'self'`, e o navegador bloquearia o envio.
6. Processar os relatórios guardados no Raio-X do consultor (hoje eles só ficam no Storage, com o lead pendente).
7. Recomendado: um papel próprio no banco para o serviço, no lugar do usuário da aplicação principal.
   - `lead_avaliacao` tem RLS, e a única política que libera acesso é a de `postgres` e `service_role` (a outra nega `anon` e `authenticated`). O papel novo precisa de políticas próprias de INSERT e de SELECT, sem BYPASSRLS: o SELECT conta o teto e confere o lead gravado. Só com GRANT, todo envio falha.
   - Também precisa de uma política de SELECT em `documento_legal` (RLS ligado, sem política) e de INSERT em `aceite_documento_lead`, e de poder travar `documento_legal` em `SHARE MODE`: o PostgreSQL só deixa o dono da tabela, quem tem UPDATE, DELETE ou TRUNCATE nela, ou MAINTAIN a partir da versão 17. Use o UPDATE: com RLS e sem política de UPDATE para o papel, ele não muda nenhuma linha. O TRUNCATE não passa pelo RLS e não serve. A conferência da subida mostra `InsufficientPrivilege` se faltar.
   - O `SUPABASE_URL` continua saindo do usuário `<papel>.<projeto>` do pooler.

### Na aplicação principal (fora do escopo autorizado desta entrega)

- **Teto do Raio-X público.** Ele conta todo lead com origem `site` do dia (`blueprints/avaliacao_publica.py`, `_avaliacoes_de_hoje`), e os do microsserviço entram nessa conta. Com o Raio-X público ligado, 20 pré-cadastros da landing esgotariam o teto dele. Sugestão: contar só os leads com `plano_interesse` preenchido, que o microsserviço nunca grava.
- **Tela do consultor.** Ela mostra o lead do microsserviço como "Raio-X público" e com o aviso de dados expirados, porque o lead não tem linhas de PDV. O lead pendente e nunca avaliado deveria aparecer como "aguardando avaliação", com a indicação de que há relatórios no bucket.
- **CI.** O passo de testes da CI (`.github/workflows/nexo-security-ci.yml`) só descobre `tests/` da raiz, e os testes deste serviço não rodam em nenhum PR. Hoje só a etapa 6d exercita o `repositorio.py`. Falta um passo com `working-directory: microservico_raiox` que rode `python -I instalar_dependencias.py` e `python -B -m unittest discover -s tests -t .`.
- **Backup.** `Docs/OPERACAO_BACKUP_RESTAURACAO.md` diz que não há Storage nos fontes, e agora há o bucket `leads-raio-x`. O inventário precisa dizer o que o `pg_dump` cobre (os caminhos no lead) e o que não cobre (os relatórios), e como copiar ou expurgar os objetos junto com o banco, em linha com a retenção do passo 1. Vale antes de o serviço receber dados.
