/* NEXO — NexoBot (widget do portal do cliente), versão 2.0
 * ------------------------------------------------------------------
 * - Todo texto do bot e do usuário é inserido como TEXTO (createTextNode):
 *   só **negrito** e quebras de linha recebem marcação, montada via DOM.
 * - Respostas estruturadas chegam como `blocos` (cabeçalho, texto, passos,
 *   seção, exemplo, aviso) e `sugestoes` (chips que apenas enviam uma
 *   mensagem). Tipos desconhecidos são ignorados; nada vira HTML.
 * - Ações vindas da API só são aceitas se o destino e a URL coincidirem
 *   exatamente com o catálogo renderizado pelo servidor e forem da mesma origem.
 * - O contexto da conversa fica apenas na memória desta aba (identificadores
 *   do catálogo e da base de conhecimento) e é revalidado pelo servidor.
 * - Mobile-first: painel em tela cheia abaixo de 640px, ajustado ao teclado
 *   virtual via visualViewport; textarea com altura automática; Enter envia.
 */
(function () {
  'use strict';

  var raiz = document.getElementById('nexobot');
  if (!raiz) return;
  var launcher = document.getElementById('nexobotLauncher'),
      panel = document.getElementById('nexobotPanel'),
      closeBtn = document.getElementById('nexobotClose'),
      msgs = document.getElementById('nexobotMsgs'),
      form = document.getElementById('nexobotForm'),
      input = document.getElementById('nexobotInput'),
      enviarBtn = document.getElementById('nexobotEnviar'),
      status = document.getElementById('nexobotStatus');
  if (!launcher || !panel || !msgs || !form || !input || !enviarBtn) return;

  var TEMPO_LIMITE_MS = 20000;
  var ALTURA_MAXIMA_INPUT = 132; /* ~5 linhas */
  var ACOES = { navegar: true, sugerir_navegacao: true };
  var TIPOS_BLOCO = { cabecalho: true, texto: true, passos: true, secao: true, exemplo: true, aviso: true };
  var CHAVES_CONTEXTO = ['ultimo_destino', 'destino_anterior', 'oferta'];
  var ASSUNTO_VALIDO = /^nexo(\.[a-z0-9_]+){2,}$/;
  var SAUDACAO = 'Olá! Sou o NexoBot, seu assistente do NEXO. Posso explicar como a plataforma funciona, ' +
                 'orientar sobre o envio de relatórios e ajudar você a encontrar as funcionalidades do portal. ' +
                 'Sobre o que você quer saber?';
  var SUGESTOES_INICIAIS = [
    { rotulo: 'Como funciona o NEXO?', mensagem: 'Como o NEXO funciona?' },
    { rotulo: 'Enviar relatórios', mensagem: 'Enviar relatórios' },
    { rotulo: 'Entender meus indicadores', mensagem: 'Entender meus indicadores' },
    { rotulo: 'Ver minhas análises', mensagem: 'Ver minhas análises' },
    { rotulo: 'Preciso de ajuda', mensagem: 'Preciso de ajuda' }
  ];
  var telaPequena = window.matchMedia ? window.matchMedia('(max-width: 639.98px)') : { matches: false };
  var movimentoReduzido = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : { matches: false };

  var endpoint = raiz.getAttribute('data-endpoint') || '';
  var destinos = {};
  try {
    var bruto = JSON.parse(raiz.getAttribute('data-destinos') || '{}');
    if (bruto && typeof bruto === 'object' && !Array.isArray(bruto)) {
      Object.keys(bruto).forEach(function (chave) {
        if (typeof bruto[chave] === 'string') destinos[chave] = bruto[chave];
      });
    }
  } catch (_) { destinos = {}; }
  if (endpoint.charAt(0) !== '/' || endpoint.charAt(1) === '/') return;

  var saudou = false, pendente = false, redirecionando = false,
      contexto = {}, requisicao = null;

  /* ---------- renderização segura ---------- */
  function texto(valor) { return typeof valor === 'string' ? valor : ''; }
  function preencher(alvo, conteudo) {
    /* Parágrafos: linhas separadas por <br>; **negrito** vira <strong>. Nada mais é interpretado. */
    String(conteudo == null ? '' : conteudo).split('\n').forEach(function (linha, i) {
      if (i) alvo.appendChild(document.createElement('br'));
      var destaque = /\*\*(.+?)\*\*/g, inicio = 0, trecho;
      while ((trecho = destaque.exec(linha)) !== null) {
        alvo.appendChild(document.createTextNode(linha.slice(inicio, trecho.index)));
        var forte = document.createElement('strong');
        forte.textContent = trecho[1];
        alvo.appendChild(forte);
        inicio = destaque.lastIndex;
      }
      alvo.appendChild(document.createTextNode(linha.slice(inicio)));
    });
  }
  function criarBloco(bloco) {
    if (!bloco || typeof bloco !== 'object' || !TIPOS_BLOCO[bloco.tipo]) return null;
    var el;
    if (bloco.tipo === 'cabecalho') {
      el = document.createElement('div');
      el.className = 'nb-cabecalho';
      el.textContent = texto(bloco.texto);
    } else if (bloco.tipo === 'texto') {
      el = document.createElement('p');
      el.className = 'nb-p';
      preencher(el, texto(bloco.texto));
    } else if (bloco.tipo === 'passos') {
      if (!Array.isArray(bloco.itens)) return null;
      el = document.createElement('div');
      el.className = 'nb-passos';
      if (bloco.titulo) {
        var titulo = document.createElement('div');
        titulo.className = 'nb-rotulo';
        titulo.textContent = texto(bloco.titulo);
        el.appendChild(titulo);
      }
      var lista = document.createElement('ol');
      bloco.itens.slice(0, 30).forEach(function (item) {
        if (typeof item !== 'string') return;
        var li = document.createElement('li');
        preencher(li, item);
        lista.appendChild(li);
      });
      el.appendChild(lista);
    } else if (bloco.tipo === 'secao' || bloco.tipo === 'exemplo') {
      el = document.createElement('div');
      el.className = bloco.tipo === 'secao' ? 'nb-secao' : 'nb-exemplo';
      var rotulo = document.createElement('div');
      rotulo.className = 'nb-rotulo';
      rotulo.textContent = texto(bloco.titulo) || (bloco.tipo === 'exemplo' ? 'Exemplo (hipotético)' : '');
      var corpo = document.createElement('p');
      corpo.className = 'nb-p';
      preencher(corpo, texto(bloco.texto));
      if (rotulo.textContent) el.appendChild(rotulo);
      el.appendChild(corpo);
    } else if (bloco.tipo === 'aviso') {
      el = document.createElement('div');
      el.className = 'nb-aviso';
      el.setAttribute('role', 'note');
      var icone = document.createElement('i');
      icone.className = 'bi bi-info-circle';
      icone.setAttribute('aria-hidden', 'true');
      var textoAviso = document.createElement('span');
      preencher(textoAviso, texto(bloco.texto));
      el.appendChild(icone);
      el.appendChild(textoAviso);
    }
    return el;
  }
  function revelar(el) {
    /* Mensagens longas: mostra o início; curtas: rola até o fim. */
    var alto = el.offsetHeight > msgs.clientHeight * 0.8;
    if (alto) msgs.scrollTop = Math.max(0, el.offsetTop - 8);
    else msgs.scrollTop = msgs.scrollHeight;
  }
  function add(conteudo, quem, extraClasse) {
    var el = document.createElement('div'), bubble = document.createElement('div');
    el.className = 'nb-msg nb-' + quem + (extraClasse ? ' ' + extraClasse : '');
    bubble.className = 'nb-bubble';
    var blocos = conteudo && typeof conteudo === 'object' && Array.isArray(conteudo.blocos)
      ? conteudo.blocos.slice(0, 30).map(criarBloco).filter(Boolean) : [];
    if (blocos.length) {
      blocos.forEach(function (b) { bubble.appendChild(b); });
    } else {
      var p = document.createElement('p');
      p.className = 'nb-p';
      preencher(p, typeof conteudo === 'string' ? conteudo : texto(conteudo && conteudo.resposta));
      bubble.appendChild(p);
    }
    el.appendChild(bubble);
    msgs.appendChild(el);
    revelar(el);
    return el;
  }
  function digitando() {
    var el = document.createElement('div'), bubble = document.createElement('div'),
        pontos = document.createElement('span'), leitor = document.createElement('span');
    el.className = 'nb-msg nb-bot nb-typing';
    bubble.className = 'nb-bubble';
    pontos.className = 'nb-dots';
    pontos.setAttribute('aria-hidden', 'true');
    for (var i = 0; i < 3; i++) pontos.appendChild(document.createElement('i'));
    leitor.className = 'visually-hidden';
    leitor.textContent = 'NexoBot está digitando';
    bubble.appendChild(pontos);
    bubble.appendChild(leitor);
    el.appendChild(bubble);
    msgs.appendChild(el);
    msgs.scrollTop = msgs.scrollHeight;
    return el;
  }
  function anunciar(mensagem) { if (status) status.textContent = mensagem; }

  /* ---------- sugestões (chips): só enviam uma mensagem ---------- */
  function chips(el, lista, classe) {
    var validas = (Array.isArray(lista) ? lista : []).filter(function (s) {
      return s && typeof s === 'object' && typeof s.rotulo === 'string' && s.rotulo.trim() &&
        typeof s.mensagem === 'string' && s.mensagem.trim();
    }).slice(0, 6);
    if (!validas.length) return;
    var grupo = document.createElement('div');
    grupo.className = 'nb-chips' + (classe ? ' ' + classe : '');
    grupo.setAttribute('role', 'group');
    grupo.setAttribute('aria-label', 'Sugestões de perguntas');
    validas.forEach(function (s) {
      var botao = document.createElement('button');
      botao.type = 'button';
      botao.className = 'nb-chip';
      botao.textContent = s.rotulo.slice(0, 60);
      var mensagem = s.mensagem.slice(0, 200);
      botao.addEventListener('click', function () {
        if (pendente || redirecionando) return;
        grupo.querySelectorAll('.nb-chip').forEach(function (c) { c.classList.add('is-usada'); });
        enviar(mensagem);
      });
      grupo.appendChild(botao);
    });
    el.querySelector('.nb-bubble').appendChild(grupo);
  }

  /* ---------- catálogo e navegação ---------- */
  function urlPermitida(destino, url) {
    if (typeof destino !== 'string' || typeof url !== 'string' ||
        !Object.prototype.hasOwnProperty.call(destinos, destino) ||
        destinos[destino] !== url || url.charAt(0) !== '/' || url.charAt(1) === '/') return null;
    try {
      var validada = new URL(url, window.location.href);
      return validada.origin === window.location.origin ? validada : null;
    } catch (_) { return null; }
  }
  function paginaAtual() {
    var atual = window.location.pathname;
    for (var chave in destinos) {
      if (Object.prototype.hasOwnProperty.call(destinos, chave) && destinos[chave] === atual) return chave;
    }
    return null;
  }
  function travarBotoes(travar) {
    msgs.querySelectorAll('.nb-navigation, .nb-opcao, .nb-chip').forEach(function (b) { b.disabled = travar; });
  }
  /* Devolve true somente quando a navegação foi iniciada. */
  function navegar(destino, url) {
    var validada = urlPermitida(destino, url);
    if (!validada || redirecionando) return false;
    if (validada.pathname === window.location.pathname) {
      add('Você já está nessa página.', 'bot');
      return false;
    }
    redirecionando = true;
    travarBotoes(true);
    if (requisicao) requisicao.abort();
    anunciar('Abrindo a página…');
    fechar(false);
    window.location.assign(validada.href);
    return true;
  }
  function botaoNavegacao(classe, rotulo, destino, url) {
    var botao = document.createElement('button');
    botao.type = 'button';
    botao.className = classe;
    botao.disabled = pendente || redirecionando;
    var icone = document.createElement('i');
    icone.className = 'bi bi-arrow-right-short';
    icone.setAttribute('aria-hidden', 'true');
    botao.appendChild(document.createTextNode(rotulo));
    botao.appendChild(icone);
    botao.addEventListener('click', function () {
      if (!pendente) navegar(destino, url);
    });
    return botao;
  }
  function sugerir(el, dados) {
    var rotulo = typeof dados.rotulo_botao === 'string' && dados.rotulo_botao.trim()
      ? dados.rotulo_botao.slice(0, 80) : 'Ir para a página';
    el.querySelector('.nb-bubble').appendChild(
      botaoNavegacao('nb-navigation', rotulo, dados.destino, dados.url));
  }
  function mostrarOpcoes(el, opcoes) {
    var validas = opcoes.filter(function (o) {
      return o && typeof o === 'object' && urlPermitida(o.destino, o.url) &&
        typeof o.rotulo_botao === 'string';
    }).slice(0, 5);
    if (!validas.length) return;
    var grupo = document.createElement('div');
    grupo.className = 'nb-opcoes';
    grupo.setAttribute('role', 'group');
    grupo.setAttribute('aria-label', 'Páginas sugeridas');
    validas.forEach(function (o) {
      grupo.appendChild(botaoNavegacao('nb-opcao', o.rotulo_botao.slice(0, 80), o.destino, o.url));
    });
    el.querySelector('.nb-bubble').appendChild(grupo);
  }

  /* ---------- contexto (somente identificadores conhecidos) ---------- */
  function chaveValida(valor) {
    return typeof valor === 'string' && Object.prototype.hasOwnProperty.call(destinos, valor) ? valor : null;
  }
  function guardarContexto(novo) {
    contexto = {};
    if (!novo || typeof novo !== 'object' || Array.isArray(novo)) return;
    CHAVES_CONTEXTO.forEach(function (chave) {
      var valor = chaveValida(novo[chave]);
      if (valor) contexto[chave] = valor;
    });
    if (Array.isArray(novo.opcoes)) {
      var opcoes = novo.opcoes.map(chaveValida).filter(Boolean).slice(0, 5);
      if (opcoes.length > 1) {
        contexto.opcoes = opcoes;
        if (novo.modo_opcoes === 'navegar' || novo.modo_opcoes === 'orientar') {
          contexto.modo_opcoes = novo.modo_opcoes;
        }
      }
    }
    if (typeof novo.assunto === 'string' && novo.assunto.length <= 80 && ASSUNTO_VALIDO.test(novo.assunto)) {
      contexto.assunto = novo.assunto;
      var nivel = novo.nivel;
      if (typeof nivel === 'number' && nivel >= 0 && nivel <= 4 && Math.floor(nivel) === nivel) {
        contexto.nivel = nivel;
      }
    }
  }

  /* ---------- campo de mensagem ---------- */
  function ajustarAltura() {
    input.style.height = 'auto';
    var altura = Math.min(input.scrollHeight, ALTURA_MAXIMA_INPUT);
    input.style.height = altura + 'px';
    input.style.overflowY = input.scrollHeight > ALTURA_MAXIMA_INPUT ? 'auto' : 'hidden';
  }
  function atualizarBotao() {
    enviarBtn.disabled = pendente || redirecionando || !input.value.trim();
  }
  input.addEventListener('input', function () { ajustarAltura(); atualizarBotao(); });
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      if (typeof form.requestSubmit === 'function') form.requestSubmit();
      else form.dispatchEvent(new Event('submit', { cancelable: true }));
    }
  });

  /* ---------- painel e teclado virtual ---------- */
  function ajustarViewport() {
    /* Em tela pequena o painel ocupa a área visível; com o teclado aberto, a
       área visível encolhe e o campo de mensagem continua acessível. */
    if (panel.hidden || !telaPequena.matches || !window.visualViewport) {
      panel.style.height = '';
      panel.style.top = '';
      return;
    }
    var vv = window.visualViewport;
    panel.style.height = Math.round(vv.height) + 'px';
    panel.style.top = Math.round(vv.offsetTop) + 'px';
    msgs.scrollTop = msgs.scrollHeight;
  }
  if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', ajustarViewport);
    window.visualViewport.addEventListener('scroll', ajustarViewport);
  }
  window.addEventListener('resize', ajustarViewport);

  function abrir() {
    panel.hidden = false;
    raiz.classList.add('is-open');
    launcher.classList.add('is-open');
    launcher.setAttribute('aria-expanded', 'true');
    if (!saudou) {
      var el = add(SAUDACAO, 'bot');
      chips(el, SUGESTOES_INICIAIS, 'nb-chips-inicial');
      saudou = true;
    }
    ajustarViewport();
    ajustarAltura();
    atualizarBotao();
    input.focus({ preventScroll: true });
  }
  function fechar(devolverFoco) {
    panel.hidden = true;
    raiz.classList.remove('is-open');
    launcher.classList.remove('is-open');
    launcher.setAttribute('aria-expanded', 'false');
    panel.style.height = '';
    panel.style.top = '';
    if (devolverFoco !== false) launcher.focus();
  }
  launcher.addEventListener('click', function () { panel.hidden ? abrir() : fechar(); });
  if (closeBtn) closeBtn.addEventListener('click', function () { fechar(); });
  panel.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { e.preventDefault(); fechar(); }
  });
  // Ao voltar pelo histórico do navegador (bfcache), o widget volta a aceitar mensagens.
  window.addEventListener('pageshow', function () {
    redirecionando = false;
    travarBotoes(pendente);
    atualizarBotao();
  });

  function definirOcupado(ocupado) {
    pendente = ocupado;
    form.setAttribute('aria-busy', ocupado ? 'true' : 'false');
    travarBotoes(ocupado || redirecionando);
    atualizarBotao();
  }

  function erroDeSessao() {
    var erro = new Error('Sessão inválida');
    erro.autenticacao = true;
    return erro;
  }

  /* ---------- envio ---------- */
  function enviar(mensagemBruta) {
    var conteudo = String(mensagemBruta == null ? '' : mensagemBruta).trim();
    if (!conteudo || pendente || redirecionando) return;
    conteudo = conteudo.slice(0, 1000);
    definirOcupado(true);
    add(conteudo, 'user');
    input.value = '';
    ajustarAltura();
    var indicador = digitando();
    anunciar('Mensagem enviada. Aguardando resposta.');
    var csrf = document.querySelector('meta[name="csrf-token"]');
    requisicao = typeof AbortController === 'function' ? new AbortController() : null;
    var esgotado = false;
    var temporizador = window.setTimeout(function () {
      esgotado = true;
      if (requisicao) requisicao.abort();
    }, TEMPO_LIMITE_MS);

    fetch(endpoint, {
      method: 'POST',
      credentials: 'same-origin',
      redirect: 'follow',
      signal: requisicao ? requisicao.signal : undefined,
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'X-CSRFToken': csrf ? csrf.getAttribute('content') : ''
      },
      body: JSON.stringify({ mensagem: conteudo, contexto: contexto, pagina_atual: paginaAtual() })
    }).then(function (r) {
      if (r.redirected || r.status === 401 || r.status === 403) throw erroDeSessao();
      var tipo = r.headers.get('content-type') || '';
      if (tipo.indexOf('application/json') === -1) {
        // HTML com status 200 no lugar do JSON = página de login após a sessão expirar.
        if (r.ok) throw erroDeSessao();
        var semJson = new Error('Falha na API');
        semJson.status = r.status;
        throw semJson;
      }
      return r.json().then(function (d) {
        if (!r.ok) {
          var falha = new Error('Falha na API');
          falha.status = r.status;
          falha.mensagem = d && typeof d.resposta === 'string' ? d.resposta : null;
          throw falha;
        }
        return d;
      });
    }).then(function (d) {
      if (redirecionando) return;
      indicador.remove();
      if (!d || typeof d !== 'object' || Array.isArray(d) || typeof d.resposta !== 'string') {
        throw new Error('Resposta inválida');
      }
      if (d.acao != null && !ACOES[d.acao]) throw new Error('Ação inválida');
      if (d.acao && !urlPermitida(d.destino, d.url)) throw new Error('Destino inválido');
      guardarContexto(d.contexto);
      if (d.acao === 'navegar') {
        // Ordem inequívoca: fecha o painel e abre a página, sem confirmação extra.
        navegar(d.destino, d.url);
        return;
      }
      var el = add(d.resposta.trim() ? d : 'Desculpe, não consegui responder. Tente de novo.', 'bot');
      if (d.acao === 'sugerir_navegacao') sugerir(el, d);
      if (Array.isArray(d.opcoes)) mostrarOpcoes(el, d.opcoes);
      if (Array.isArray(d.sugestoes)) chips(el, d.sugestoes);
      revelar(el);
      anunciar('Nova resposta do NexoBot.');
    }).catch(function (erro) {
      indicador.remove();
      if (redirecionando) return;
      contexto = {};
      if (erro && erro.autenticacao) {
        msgs.querySelectorAll('.nb-navigation, .nb-opcao, .nb-chip').forEach(function (b) { b.remove(); });
        add('Sua sessão expirou ou não permite usar o NexoBot. Atualize a página para entrar novamente.',
            'bot', 'nb-erro');
        return;
      }
      if (!input.value) {
        input.value = conteudo;  // permite reenviar sem digitar de novo
        ajustarAltura();
      }
      var mensagem;
      if (esgotado) mensagem = 'A resposta demorou mais que o esperado. Tente enviar novamente.';
      else if (erro && erro.status === 429) mensagem = erro.mensagem || 'Muitas mensagens em pouco tempo. Aguarde um minuto.';
      else if (erro && erro.status && erro.status < 500 && erro.mensagem) mensagem = erro.mensagem;
      else mensagem = 'Não consegui responder agora. Tente novamente ou abra um chamado em “Suporte”.';
      var el = add(mensagem, 'bot', 'nb-erro');
      if (!(erro && erro.status === 429)) {
        chips(el, [{ rotulo: 'Tentar novamente', mensagem: conteudo }]);
      }
    }).finally(function () {
      window.clearTimeout(temporizador);
      indicador.remove();
      requisicao = null;
      definirOcupado(false);
      if (!redirecionando && !panel.hidden) input.focus({ preventScroll: true });
    });
  }

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    enviar(input.value);
  });

  if (movimentoReduzido.matches) raiz.classList.add('nb-sem-movimento');

  /* Rolando para baixo, o botão sai de cena e não cobre a linha ou o botão que passa por baixo
     dele; volta ao rolar para cima, perto do fim da página (onde há espaço reservado) e com o
     foco do teclado. Com o painel aberto, nada muda. */
  var ultimoY = window.scrollY || 0, quadro = null;
  function acompanharRolagem() {
    quadro = null;
    var y = window.scrollY || 0;
    var pertoDoFim = y + window.innerHeight >= document.documentElement.scrollHeight - 120;
    if (!panel.hidden || pertoDoFim || y < 80 || y < ultimoY - 4) raiz.classList.remove('is-recolhido');
    else if (y > ultimoY + 4 && document.activeElement !== launcher) raiz.classList.add('is-recolhido');
    if (Math.abs(y - ultimoY) > 4) ultimoY = y;
  }
  window.addEventListener('scroll', function () {
    if (!quadro) quadro = window.requestAnimationFrame(acompanharRolagem);
  }, { passive: true });
  launcher.addEventListener('focus', function () { raiz.classList.remove('is-recolhido'); });
})();
