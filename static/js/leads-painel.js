/*
 * NEXO — Painel lateral da Avaliação de Cenário (lista de leads do consultor)
 * ==========================================================================
 * Na lista (templates/admin/leads.html), o "Ver Avaliação de Cenário" de cada linha é um link para a página completa
 * do lead: sem JavaScript, ou com o clique modificado (Ctrl, Shift, botão do meio), é ela que abre. Com JavaScript, o
 * clique abre o <dialog id="painelLead"> à direita e busca o fragmento do lead (data-painel-url, GET
 * /admin/leads/<id>/painel):
 *   - fetch na própria origem, com o cookie da sessão e SEM seguir redirecionamento: a sessão vencida (o servidor
 *     manda para o login) vira o aviso "Sua sessão expirou: recarregue a página", e a tela de login nunca entra no
 *     painel. Só entra a resposta HTML marcada pelo servidor (X-Nexo-Painel), que o Jinja já escapou; o resto (404,
 *     403, erro, rede) vira um aviso escrito por este script, por textContent;
 *   - enquanto carrega, o corpo fica com aria-busy e um aviso discreto (role="status") diz o que acontece; no fim, o
 *     título ganha o nome do lead e recebe o foco;
 *   - Esc, o botão de fechar e o clique no fundo fecham, e o foco volta ao botão que abriu; Tab e Shift+Tab ficam
 *     no painel;
 *   - a lista que chega com data-painel-abrir (o servidor o põe a partir de ?ver=<id>, a volta do processamento)
 *     abre o painel daquele lead ao carregar; ao fechar, o ?ver= sai do endereço, para recarregar não abrir de novo.
 * Na lista e na página do lead, o formulário de processar as planilhas (data-processar-planilhas) deixa o botão
 * ocupado enquanto elas são lidas (até dois minutos), como o planos.js faz nos envios com arquivo.
 * Nada vem de fora da página: nenhum valor do servidor é executado, e nada é guardado no navegador.
 */
(function () {
  'use strict';

  var MENSAGENS = {
    carregando: 'Carregando a Avaliação de Cenário…',
    carregado: 'Avaliação de Cenário carregada.',
    sessao: 'Sua sessão expirou: recarregue a página.',
    naoEncontrado: 'Este lead não foi encontrado: recarregue a lista.',
    falha: 'Não foi possível carregar a avaliação agora. Tente de novo ou use "Abrir página completa".',
    processando: 'Processando os relatórios…'
  };
  var TEMPO_LIMITE_MS = 30000;
  var PRAZO_DO_PROCESSAMENTO_MS = 150000;

  function focar(elemento) {
    if (!elemento) return;
    try { elemento.focus({ preventScroll: true }); } catch (erro) { elemento.focus(); }
  }

  // Só caminho local do próprio site (nada de //host ou /\host).
  function caminhoLocal(valor) {
    return typeof valor === 'string' && /^\/(?![\/\\])/.test(valor) ? valor : '';
  }

  // ------------------------------------------------------------------
  // Processar as planilhas: o botão fica ocupado até a resposta (os envios com dropzone ficam com o planos.js).
  // ------------------------------------------------------------------
  document.addEventListener('submit', function (evento) {
    var form = evento.target;
    if (!form || !form.hasAttribute || !form.hasAttribute('data-processar-planilhas') || evento.defaultPrevented) return;
    var botao = form.querySelector('button[type="submit"]');
    if (!botao || botao.disabled) return;
    var original = botao.innerHTML;
    botao.disabled = true;
    botao.setAttribute('aria-busy', 'true');
    botao.textContent = MENSAGENS.processando;
    // Se o envio for interrompido (Esc, Parar), o botão se solta depois do prazo do processamento.
    window.setTimeout(function () {
      botao.disabled = false;
      botao.removeAttribute('aria-busy');
      botao.innerHTML = original;
    }, PRAZO_DO_PROCESSAMENTO_MS);
  });

  // ------------------------------------------------------------------
  // O painel lateral (só na lista)
  // ------------------------------------------------------------------
  var dialogo = document.getElementById('painelLead');
  if (!dialogo || typeof dialogo.showModal !== 'function' || !window.fetch) return;
  // Incluído duas vezes, o script não liga tudo em dobro.
  if (dialogo.hasAttribute('data-painel-pronto')) return;

  var corpo = dialogo.querySelector('[data-painel-corpo]');
  var titulo = document.getElementById('painelLeadTitulo');
  var estado = dialogo.querySelector('[data-painel-estado]');
  var linkPagina = dialogo.querySelector('[data-painel-pagina]');
  var avisos = dialogo.querySelector('[data-painel-avisos]');
  var botaoFechar = dialogo.querySelector('[data-painel-fechar]');
  if (!corpo || !titulo || !estado) return;
  dialogo.setAttribute('data-painel-pronto', '');

  var TITULO = titulo.textContent;
  var gatilho = null;
  var pedido = 0;
  var controle = null;

  function ocupado(sim) {
    corpo.setAttribute('aria-busy', sim ? 'true' : 'false');
  }

  function avisar(texto) {
    corpo.textContent = '';
    var caixa = document.createElement('div');
    caixa.className = 'aviso-contrato is-alerta';
    caixa.setAttribute('role', 'alert');
    var icone = document.createElement('i');
    icone.className = 'bi bi-exclamation-triangle';
    icone.setAttribute('aria-hidden', 'true');
    var conteudo = document.createElement('div');
    var paragrafo = document.createElement('p');
    paragrafo.textContent = texto;
    conteudo.appendChild(paragrafo);
    caixa.appendChild(icone);
    caixa.appendChild(conteudo);
    corpo.appendChild(caixa);
    ocupado(false);
    estado.textContent = '';
    focar(titulo);
  }

  function carregando() {
    corpo.textContent = '';
    var linha = document.createElement('p');
    linha.className = 'painel-lead-carregando';
    var girador = document.createElement('span');
    girador.className = 'painel-lead-girador';
    girador.setAttribute('aria-hidden', 'true');
    linha.appendChild(girador);
    linha.appendChild(document.createTextNode(MENSAGENS.carregando));
    corpo.appendChild(linha);
    ocupado(true);
    estado.textContent = MENSAGENS.carregando;
  }

  function injetar(html) {
    // O fragmento é HTML do próprio servidor, marcado e escapado pelo Jinja (o innerHTML não executa script).
    corpo.innerHTML = html;
    var raiz = corpo.querySelector('[data-painel-fragmento]');
    var nome = raiz ? raiz.getAttribute('data-painel-titulo') : '';
    titulo.textContent = nome ? TITULO + ': ' + nome : TITULO;
    var pagina = raiz ? caminhoLocal(raiz.getAttribute('data-painel-pagina') || '') : '';
    if (pagina && linkPagina) linkPagina.setAttribute('href', pagina);
    ocupado(false);
    estado.textContent = MENSAGENS.carregado;
    // As tabelas largas viram cartões e os KPIs se ajustam como no resto do portal (base.html).
    if (window.NEXO && typeof window.NEXO.ajustarLayout === 'function') window.NEXO.ajustarLayout();
    corpo.scrollTop = 0;
    focar(titulo);
  }

  function buscar(url) {
    var meu = ++pedido;
    if (controle) controle.abort();
    controle = window.AbortController ? new AbortController() : null;
    var esteControle = controle;
    var limite = esteControle ? window.setTimeout(function () { esteControle.abort(); }, TEMPO_LIMITE_MS) : null;
    carregando();
    fetch(url, {
      method: 'GET',
      credentials: 'same-origin',
      redirect: 'manual',
      cache: 'no-store',
      headers: { 'Accept': 'text/html', 'X-Requested-With': 'fetch' },
      signal: esteControle ? esteControle.signal : undefined
    }).then(function (resposta) {
      if (meu !== pedido) return null;
      // Redirecionamento (o login da sessão vencida) não é seguido: chega opaco, sem status.
      if (resposta.type === 'opaqueredirect' || resposta.redirected || resposta.status === 0) return MENSAGENS.sessao;
      var tipo = resposta.headers.get('Content-Type') || '';
      var doPainel = resposta.headers.get('X-Nexo-Painel') === '1';
      if (doPainel && tipo.indexOf('text/html') === 0 && (resposta.ok || resposta.status === 503)) {
        return resposta.text().then(function (html) {
          if (meu === pedido) injetar(html);
          return null;
        });
      }
      if (resposta.status === 404) return MENSAGENS.naoEncontrado;
      if (resposta.status === 401 || resposta.status === 403) return MENSAGENS.sessao;
      return MENSAGENS.falha;
    }).then(function (mensagem) {
      if (mensagem && meu === pedido) avisar(mensagem);
    }, function () {
      // Rede, prazo vencido ou o painel fechado no meio (aí o pedido já é outro e nada muda).
      if (meu === pedido) avisar(MENSAGENS.falha);
    }).then(function () {
      window.clearTimeout(limite);
    });
  }

  function abrir(url, origem) {
    var caminho = caminhoLocal(url);
    if (!caminho) return false;
    gatilho = origem || null;
    titulo.textContent = TITULO;
    var pagina = origem ? caminhoLocal(origem.getAttribute('href') || '') : '';
    if (pagina && linkPagina) linkPagina.setAttribute('href', pagina);
    // As mensagens da volta do processamento valem só para o painel que abriu com a página.
    if (avisos && origem) avisos.hidden = true;
    if (!dialogo.open) {
      dialogo.showModal();
      document.documentElement.classList.add('painel-lead-aberto');
    }
    focar(titulo);
    buscar(caminho);
    return true;
  }

  function fechar() {
    if (dialogo.open) dialogo.close();
  }

  function tirarVerDoEndereco() {
    if (!window.history || typeof window.history.replaceState !== 'function' || !window.URL) return;
    try {
      var endereco = new URL(window.location.href);
      if (!endereco.searchParams.has('ver')) return;
      endereco.searchParams.delete('ver');
      window.history.replaceState(window.history.state, '', endereco.pathname + endereco.search + endereco.hash);
    } catch (erro) { /* o endereço fica como está */ }
  }

  dialogo.addEventListener('close', function () {
    pedido++;
    if (controle) controle.abort();
    controle = null;
    document.documentElement.classList.remove('painel-lead-aberto');
    corpo.textContent = '';
    ocupado(false);
    estado.textContent = '';
    titulo.textContent = TITULO;
    if (avisos) avisos.hidden = true;
    tirarVerDoEndereco();
    var destino = gatilho && document.body.contains(gatilho) ? gatilho : null;
    gatilho = null;
    if (destino) focar(destino);
  });

  if (botaoFechar) botaoFechar.addEventListener('click', fechar);

  // Clique no fundo escurecido (fora da caixa) fecha. Só quando o clique COMEÇOU no fundo: selecionar um texto e
  // soltar o mouse fora da caixa não fecha.
  var cliqueComecouNoFundo = false;
  dialogo.addEventListener('pointerdown', function (evento) {
    cliqueComecouNoFundo = evento.target === dialogo;
  });
  dialogo.addEventListener('click', function (evento) {
    if (evento.target === dialogo && cliqueComecouNoFundo) fechar();
    cliqueComecouNoFundo = false;
  });

  // O que recebe Tab dentro do painel, visível e habilitado.
  function focaveis() {
    var candidatos = dialogo.querySelectorAll('a[href], button, input, select, textarea, [tabindex="0"]');
    return Array.prototype.filter.call(candidatos, function (elemento) {
      return !elemento.disabled && elemento.getAttribute('tabindex') !== '-1' && elemento.type !== 'hidden' &&
        elemento.getClientRects().length > 0;
    });
  }

  dialogo.addEventListener('keydown', function (evento) {
    if (evento.key !== 'Tab') return;
    var lista = focaveis();
    if (!lista.length) return;
    var primeiro = lista[0], ultimo = lista[lista.length - 1], ativo = document.activeElement;
    var dentro = dialogo.contains(ativo) && ativo !== dialogo;
    if (evento.shiftKey && (!dentro || ativo === primeiro || ativo === titulo)) {
      evento.preventDefault();
      focar(ultimo);
    } else if (!evento.shiftKey && (!dentro || ativo === ultimo)) {
      evento.preventDefault();
      focar(primeiro);
    }
  });

  // Os "Ver Avaliação de Cenário" passam a abrir o painel (o href da página completa fica como reserva).
  function gatilhos() {
    return Array.prototype.slice.call(document.querySelectorAll('a[data-painel-url]')).filter(function (link) {
      return !dialogo.contains(link);
    });
  }

  gatilhos().forEach(function (link) {
    link.setAttribute('role', 'button');
    link.setAttribute('aria-haspopup', 'dialog');
    link.setAttribute('aria-controls', 'painelLead');
  });

  document.addEventListener('click', function (evento) {
    var link = evento.target && evento.target.closest ? evento.target.closest('a[data-painel-url]') : null;
    if (!link || dialogo.contains(link) || evento.defaultPrevented) return;
    if (evento.button !== 0 || evento.metaKey || evento.ctrlKey || evento.shiftKey || evento.altKey) return;
    try {
      if (abrir(link.getAttribute('data-painel-url'), link)) evento.preventDefault();
    } catch (erro) {
      // Sem o painel, o link segue para a página completa.
    }
  });

  document.addEventListener('keydown', function (evento) {
    if (evento.key !== ' ' && evento.key !== 'Spacebar') return;
    var link = evento.target && evento.target.closest ? evento.target.closest('a[data-painel-url]') : null;
    if (!link || dialogo.contains(link)) return;
    evento.preventDefault();
    link.click();
  });

  // A volta do processamento (?ver=<id>, validado pelo servidor): o painel daquele lead abre ao carregar.
  var abrirAoCarregar = caminhoLocal(dialogo.getAttribute('data-painel-abrir') || '');
  if (abrirAoCarregar) {
    var botao = gatilhos().filter(function (link) {
      return link.getAttribute('data-painel-url') === abrirAoCarregar;
    })[0] || null;
    try {
      abrir(abrirAoCarregar, null);
      gatilho = botao;
    } catch (erro) { /* a lista continua disponível */ }
  }
})();
