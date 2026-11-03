# 03 — Matriz de acessos

Toda a autorização é decidida no servidor. "Pago" significa que o período inteiro da análise (o declarado e o real da base) está dentro das coberturas pagas não revogadas (documento 02, §4). Para o regime **legado**, valem as colunas de ATIVA/SUSPENSA com acesso integral ao histórico publicado e ao PDF, como antes. A coluna CANCELADO também vale para o legado.

Legenda: ✅ permitido · ❌ negado · ⚠️ permitido com restrição.

| Recurso | ADMIN | CLIENTE ATIVO (regime contratual) | CLIENTE SUSPENSO dentro do prazo de PDF | CLIENTE SUSPENSO após o prazo de PDF | CLIENTE com suspensão expirada | CLIENTE CANCELADO |
|---|---|---|---|---|---|---|
| **Login** | ✅ | ✅ | ✅ | ✅ | ❌ Com a senha correta, mostra a página "Conta encerrada" (sem sessão). Com a senha errada, só a mensagem genérica | ❌ Com a senha correta, mostra "Conta encerrada" (sem sessão). Com a senha errada, só a mensagem genérica |
| **Sessão já aberta** | ✅ | ✅ | ✅ | ✅ | ❌ Derrubada na próxima requisição (302 para o login) | ❌ Revogada (geração +1) e derrubada |
| **Dashboard** | ✅ (admin) | ⚠️ Somente análises pagas; o gráfico histórico usa só as pagas | ⚠️ Somente as pagas, com aviso da suspensão e das datas-limite | ⚠️ Somente as pagas, com aviso de que o prazo de PDF terminou | ❌ | ❌ |
| **Histórico** | ✅ (admin, todas) | ⚠️ Somente as pagas | ⚠️ Somente as pagas | ⚠️ Somente as pagas | ❌ | ❌ |
| **Análise `/cliente/analise/<id>`** | n/a (usa as telas admin) | ⚠️ Paga: 200; não paga: 403; de outra empresa ou não publicada: 404 | ⚠️ Igual à coluna anterior | ⚠️ Igual, com aviso de PDF encerrado e sem botão | ❌ | ❌ |
| **PDF `/cliente/analise/<id>/pdf`** | n/a | ⚠️ Paga: 200; não paga: 403 | ⚠️ Paga: 200; não paga: 403 | ❌ 403 com a mensagem de prazo encerrado; o PDF não é gerado | ❌ | ❌ |
| **Uploads `/cliente/upload`** | ✅ (homologação admin) | ✅ Análises abertas | ⚠️ Somente análise aberta de período pago | ⚠️ Somente análise aberta de período pago | ❌ | ❌ |
| **Nova análise (admin)** | ✅ Exige empresa ATIVA e período dentro da vigência do contrato | — | ❌ | ❌ | ❌ | ❌ |
| **Suporte (tickets)** | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ |
| **Notificações (sininho, Socket.IO)** | ✅ | ✅ A publicação de análise não paga não gera aviso ao cliente | ✅ (mesmo critério) | ✅ (mesmo critério) | ❌ A entrega é recusada | ❌ |
| **E-mail de publicação** | — | ⚠️ Só se o cliente puder abrir a análise | ⚠️ Idem | ⚠️ Idem | ❌ | ❌ (antes, o legado ainda enviava) |
| **APIs (`/api/suporte-bot`)** | ❌ (somente CLIENTE) | ✅ | ✅ | ✅ | ❌ Sessão derrubada | ❌ |
| **Socket.IO (handshake e eventos)** | ✅ | ✅ | ✅ | ✅ | ❌ Recusado; a varredura derruba | ❌ |
| **Recuperação e definição de senha** | ✅ | ✅ | ✅ | ✅ | ❌ Mensagem genérica, nenhum e-mail; tokens anteriores recusados | ❌ Idem |
| **Primeiro acesso por token** | — | ✅ | ✅ | ✅ | ❌ | ❌ |
| **Telas de contratos `/admin/empresas/<id>/detalhes` e `/admin/contratos`** | ✅ | ❌ 403 | ❌ 403 | ❌ 403 | ❌ | ❌ |

## Observações

- **"Suspensão expirada"** vale a partir do instante-limite, mesmo antes da reconciliação. Depois da reconciliação, a empresa passa a CANCELADA, e a coluna é a mesma.
- **Regras ativas sem contrato vigente** (por exemplo, status alterado fora do fluxo contratual) têm o mesmo tratamento da suspensão expirada: portal negado e página "Conta encerrada".
- **Empresa na lixeira e usuário inativo** continuam com a mensagem genérica "Esta conta está desativada". A página de conta encerrada não aparece nesses casos.
- **A página "Conta encerrada" não revela a situação a terceiros.**
  - Ela só aparece depois de a senha correta ser validada, sob o mesmo limite de taxa do login.
  - E-mails inexistentes passam a consumir o mesmo hash, o que elimina a diferença de tempo.
  - Ela não grava sessão nem flash, não redireciona e envia `Cache-Control: no-store`.
  - Usa o layout de autenticação, sem `current_user` nem CSRF, e não carrega dados da empresa.
- **Canais de contato:** não há e-mail, telefone ou WhatsApp oficialmente configurado. A página indica apenas "entre em contato com a equipe NEXO" e oferece um link para "Como contratar a NEXO" (`/como-aderir`).
- **Aviso de divergência com o total do PDV no Dashboard:** é uma exceção à regra de só mostrar número depois da publicação. Em período liberado pelo contrato, o cliente vê o valor informado, o calculado e a diferença, como preliminares; em período não pago, só o total digitado por um usuário da própria empresa. Ver `Docs/TRAVA_TOTAL_PDV.md`.
