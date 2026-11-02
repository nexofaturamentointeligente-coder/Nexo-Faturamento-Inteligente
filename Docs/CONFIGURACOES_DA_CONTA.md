# Configurações da conta

Módulo em `/cliente/configuracoes` e `/admin/configuracoes`. As regras de negócio ficam em
`configuracoes_conta.py` e `preferencias_usuario.py`; a troca de senha e de e-mail, em
`blueprints/auth.py`, junto do restante da credencial.

## Regras de segurança

- Toda alteração é POST com CSRF, exige o perfil certo (`cliente_required` ou `admin_required`) e tem
  limite por usuário. Nenhuma rota do cliente recebe identificador de empresa ou de usuário: tudo parte da sessão.
- **Senha:** exige a senha atual e passa pela política de `politica_senha.py` (mínimo de 12 caracteres),
  somada aqui à exigência de letras e números. A troca é transacional: as outras sessões caem e esta é
  renovada. O dono recebe um aviso por e-mail.
- **E-mail de acesso:** exige a senha atual e a digitação do novo endereço duas vezes. A geração de autorização
  sobe na mesma transação, então as outras sessões e os links de senha já enviados deixam de valer. O endereço
  antigo recebe um aviso. O novo endereço não é confirmado por link, e por isso esta rota revela a um usuário
  autenticado, com a própria senha e no limite de tentativas, se um endereço já tem conta: risco aceito enquanto
  não houver confirmação por link. O e-mail de contato da empresa não muda.
- **Retomada pelo suporte:** em Empresas, Detalhes, *Corrigir e-mail de acesso* exige a senha do administrador e o
  novo endereço digitado duas vezes, e só vale para conta com acesso ao portal. A ação grava o novo endereço, derruba
  as sessões, invalida a senha atual e envia o link para definir outra. O endereço anterior recebe um aviso sem link;
  se a conta ainda não foi ativada, o aviso só diz que o convite foi cancelado, porque quem lê pode ser um estranho.
  Os avisos de troca pedem ao dono que responda ao e-mail: a caixa do remetente precisa ser acompanhada. Com o envio
  indisponível a correção vale assim mesmo, para tirar o acesso de quem tomou a conta: o link não sai, e o dono usa
  “Esqueci minha senha” quando o envio voltar.
- **Reautenticação:** a troca de senha, a de e-mail e a caixa de cópia de chamados somam o mesmo limite, 10
  tentativas por 15 minutos.
- **Exportação LGPD:** lista explícita de campos, só da empresa da sessão: conta, aceites, preferências, empresa,
  contratos, os chamados abertos pela própria pessoa e análises. Análises seguem a regra do PDF
  (`pode_exportar_pdf`) e usam a cópia publicada, nunca o rascunho. Resposta com `Cache-Control: no-store`.
- **Exclusão de dados:** o botão abre um chamado com prioridade alta e não apaga nada. Com um chamado aberto,
  não abre outro.
- **Documentos legais:** texto puro, sempre escapado. Cada publicação é uma versão imutável, e cada aceite
  aponta para a versão lida. Nenhum texto jurídico vem pronto no código: até o administrador publicar, a tela
  informa que o documento não foi publicado.

## Avisos

| Aviso | Pode desligar? | Canais |
| --- | --- | --- |
| Senha alterada, e-mail alterado | Não | E-mail |
| Chamado respondido | Sim (`alerta_chamado`) | Sininho |
| Chamado resolvido | Sim (`alerta_chamado`) | Sininho e e-mail |
| Renovação do contrato | Sim (`lembrete_renovacao`) | Faixa na Visão Geral, sininho e e-mail |

Sem linha em `preferencia_usuario`, tudo fica ligado. O e-mail de contato da empresa não pertence a um usuário:
ele só deixa de receber o aviso de chamado resolvido se for o mesmo endereço de quem desligou o alerta.

O lembrete de renovação vale para contratos ATIVOS de empresas no regime contratual, até
`CONTRATO_AVISO_RENOVACAO_DIAS` (14) dias antes do fim da vigência. A faixa é automática. O sininho e o e-mail
dependem do botão em Contratos ou de um comando, porque não há agendador configurado. Um e-mail não aceito não
conta: a pessoa volta a ser avisada na execução seguinte, e o comando termina com código 1. Endereço recusado em
definitivo (inválido ou caixa inexistente) fica só com o sininho, sem nova tentativa até a próxima data de término, e
o comando termina com código 1 nessa rodada: ele lista empresa e usuário, e a tela cita os nomes. Recusa que fala do
remetente ou da política do servidor continua pendente, porque corrigir o remetente resolve. A rodada grava e envia
uma pessoa por vez e confere de novo o contrato e a pessoa antes de cada uma: se parar por erro, quem não foi tentado
continua pendente. Quem estava em andamento fica com o sininho e a faixa, sem o e-mail.

```
flask contratos lembretes-renovacao            # simulação: lista quem seria avisado
flask contratos lembretes-renovacao --aplicar  # avisa uma vez por pessoa e por data de término
```

## Cópia de novos chamados

Com a caixa definida em `/admin/configuracoes`, cada novo chamado gera um e-mail em cópia oculta (BCC). O
destinatário visível é o próprio remetente do NEXO, porque o provedor exige um. Sem caixa definida, nenhum
e-mail sai e os chamados continuam avisando os administradores só pelo sininho. Definir, trocar ou desligar a caixa
exige a senha do administrador e avisa todos os administradores no sininho: ela recebe o texto dos chamados e
continuaria recebendo depois que uma sessão sequestrada caísse.

## Implantação

A revisão `c7d41e9a2b60` só cria tabelas: `preferencia_usuario`, `documento_legal`, `aceite_documento` e
`configuracao_sistema`. No PostgreSQL elas nascem com RLS ligado e sem política, o que fecha a API pública do
Supabase e não afeta a aplicação, que entra como dona das tabelas.

O código funciona antes da migração: perfil, senha, e-mail, exportação, pedido de exclusão, tema e status do
SMTP já operam, e preferências, documentos e cópia de chamados informam que ainda não estão disponíveis. A
ausência das tabelas é reavaliada a cada 30 segundos, sem reiniciar o processo.

```
NEXO_DB_DIRECT=1 flask db upgrade c7d41e9a2b60
```

O downgrade (`NEXO_DB_DIRECT=1 flask db downgrade b6935e8de9d4`) recusa-se a apagar dados: só remove as tabelas se
estiverem vazias. Rode-o de uma cópia que ainda tenha o arquivo da migração: a partir do código anterior, o Alembic
responde `Can't locate revision` e nada muda. Em seguida, reinicie o serviço ou publique o código anterior. Até lá,
com o código novo no ar, falham as Configurações do cliente e do admin, a exportação de dados, as respostas de
chamado do admin e, com contrato na janela de aviso, a Visão Geral do cliente e a tela de Contratos, porque a
presença das tabelas fica guardada.

Antes de implantar, confira com uma consulta só de leitura se há endereço gravado fora de ASCII, porque a validação
agora os recusa também no envio. No contato, abrir e salvar a empresa grava o domínio em `xn--`; no usuário, use
*Corrigir e-mail de acesso*:

```sql
SELECT id_usuario FROM usuario WHERE email ~ '[^[:ascii:]]';
SELECT id_empresa FROM empresa WHERE email_contato ~ '[^[:ascii:]]';
```

## Tema

O botão no topo guarda `theme=dark` ou `theme=light` em `localStorage`, e um script no `<head>` aplica
`data-theme` e `data-bs-theme` no `<html>` antes da primeira pintura. O tema escuro é o padrão e continua em
`static/css/nexo.css`; o claro fica só em `static/css/nexo-tema-claro.css`. O tema vale para o portal
autenticado: login, landing e páginas de erro continuam escuros, e a sidebar segue escura nos dois temas.

## Pendências

- Empresa com histórico contratual não pode ser excluída pelo painel, inclusive quando o pedido vem de um chamado
  de exclusão: a regra de retenção é o item 17 de `Docs/GESTAO_CONTRATUAL/05_PENDENCIAS_DE_DECISAO_COMERCIAL.md`.
- A caixa da cópia de chamados recebe o texto dos chamados e fica fora do expurgo feito pelo sistema.
- Avisos gravados antes desta versão, de análises e chamados já apagados, continuam no sininho dos admins com o nome
  da empresa. Limpeza única, depois de conferir a contagem:

  ```sql
  DELETE FROM notificacao WHERE link_destino LIKE '/admin/analise/%/upload' AND NOT EXISTS (
    SELECT 1 FROM analise a WHERE notificacao.link_destino = '/admin/analise/' || a.id_analise || '/upload');
  DELETE FROM notificacao WHERE link_destino LIKE '/admin/tickets/%' AND NOT EXISTS (
    SELECT 1 FROM chamado_suporte c WHERE notificacao.link_destino = '/admin/tickets/' || c.id_chamado);
  ```
- O primeiro acesso não exige o aceite dos documentos: o aceite é registrado em Configurações. Exigi-lo no
  primeiro acesso é uma decisão jurídica e comercial.
- A landing informa que os documentos ainda não estão disponíveis nela: a página não lê o que o administrador
  publica, e exibi-los ali é uma decisão à parte.
- O módulo de avaliações, ainda fora desta linha de trabalho, tem uma migração com o mesmo pai (`b6935e8de9d4`) e
  seções numeradas em `models.py`. Ao integrá-lo, reaponte a migração que ainda não tiver sido aplicada, para não
  criar duas cabeças, e renumere as seções.
- Campo de texto longo que recebe o foco com o topo sob a topbar (Shift+Tab, por exemplo) não rola sozinho: os demais
  campos já rolam. Só um script resolveria, e ele moveria a página também no clique do mouse.
- O tópico “Como altero minha senha de acesso?” do Guia já gravado no banco não muda com o `seed_guia.py`, que só
  insere o que falta. Ajuste o texto em `/admin/guia`.
