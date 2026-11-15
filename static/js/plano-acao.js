/* Plano de Ação (12 meses) do Raio-X do PDV e da Auditoria de custos (Etapa 2, 14/11/2026).
   O servidor manda tudo pronto (plano_acao.py e templates/_plano_acao.html); aqui não se calcula
   nenhum valor. Este script cuida só de:
   - o checklist das 3 etapas: marcado neste navegador, por empresa e por Raio-X (localStorage, com a
     chave "nexo.planoAcao.<empresa>.<data do Raio-X>"); um Raio-X novo começa um checklist novo e o
     antigo é apagado. Cada marcação relê o que está salvo e muda só a sua etapa (o Raio-X e a Auditoria
     abertos em duas abas não apagam a marcação um do outro), e a outra aba acompanha pelo evento
     'storage'. Sem localStorage (navegação privada, bloqueio), o checklist funciona e só não fica salvo.
     A etapa já regularizada vem marcada e travada do servidor;
   - a barra de progresso e o selo "Concluída"/"Pendente" de cada etapa;
   - a impressão: o modal sai do layout (filho direto do <body>) e, aberto, a folha de impressão
     esconde o resto da página (body.plano-acao-aberto em static/css/nexo.css);
   - os links para as listas completas: na mesma página, fecha o modal e rola até a lista. */
(function () {
  'use strict';

  var modal = document.querySelector('[data-plano-acao]');
  if (!modal) return;
  if (modal.parentNode !== document.body) document.body.appendChild(modal);

  var base = modal.getAttribute('data-plano-chave') || 'nexo.planoAcao';
  var chave = base + '.' + (modal.getAttribute('data-plano-diagnostico') || '');
  var caixas = Array.prototype.slice.call(modal.querySelectorAll('input[data-plano-etapa]'));
  var barra = modal.querySelector('[data-plano-barra]');
  var cheia = barra ? barra.querySelector('.plano-acao-barra-cheia') : null;
  var textoProgresso = modal.querySelector('[data-plano-progresso-texto]');

  function armazenamento() {
    try { return window.localStorage; } catch (erro) { return null; }
  }

  function ler() {
    var local = armazenamento();
    if (!local) return {};
    try {
      var dados = JSON.parse(local.getItem(chave) || '{}');
      return dados && typeof dados === 'object' && !Array.isArray(dados) ? dados : {};
    } catch (erro) {
      return {};
    }
  }

  function gravar(estado) {
    var local = armazenamento();
    if (!local) return;
    try { local.setItem(chave, JSON.stringify(estado)); } catch (erro) { /* cota cheia ou bloqueio: só não salva */ }
  }

  // O checklist de um Raio-X anterior da mesma empresa não serve mais: o plano novo tem outros produtos.
  function apagarAntigos() {
    var local = armazenamento();
    if (!local) return;
    try {
      for (var i = local.length - 1; i >= 0; i -= 1) {
        var nome = local.key(i);
        if (nome && nome !== chave && nome.indexOf(base + '.') === 0) local.removeItem(nome);
      }
    } catch (erro) { /* sem acesso: nada a apagar */ }
  }

  function atualizar() {
    var feitas = 0;
    caixas.forEach(function (caixa) {
      var numero = caixa.getAttribute('data-plano-etapa');
      if (caixa.checked) feitas += 1;
      var selo = modal.querySelector('[data-plano-status="' + numero + '"]');
      if (selo) {
        selo.textContent = caixa.checked ? 'Concluída' : 'Pendente';
        selo.classList.toggle('b-green', caixa.checked);
        selo.classList.toggle('b-amber', !caixa.checked);
      }
      var cartao = modal.querySelector('[data-plano-etapa-cartao="' + numero + '"]');
      if (cartao) cartao.classList.toggle('is-concluida', caixa.checked);
    });
    var total = caixas.length;
    if (barra) barra.setAttribute('aria-valuenow', String(feitas));
    if (cheia) cheia.style.width = (total ? Math.round(100 * feitas / total) : 0) + '%';
    if (textoProgresso) {
      textoProgresso.textContent = feitas + ' de ' + total + (total === 1 ? ' etapa concluída' : ' etapas concluídas');
    }
  }

  function aplicar(estado) {
    caixas.forEach(function (caixa) {
      if (!caixa.disabled) caixa.checked = estado[caixa.getAttribute('data-plano-etapa')] === true;
    });
    atualizar();
  }

  caixas.forEach(function (caixa) {
    var numero = caixa.getAttribute('data-plano-etapa');
    caixa.addEventListener('change', function () {
      if (caixa.disabled) return;
      var estado = ler();
      estado[numero] = caixa.checked;
      gravar(estado);
      atualizar();
    });
  });
  // Outra aba (o Raio-X e a Auditoria usam a mesma chave) marcou ou desmarcou uma etapa.
  window.addEventListener('storage', function (evento) {
    if (evento.key === chave) aplicar(ler());
  });
  apagarAntigos();
  aplicar(ler());

  // Impressão: só o plano (a folha de impressão olha a classe no <body> enquanto o modal está aberto).
  modal.addEventListener('show.bs.modal', function () { document.body.classList.add('plano-acao-aberto'); });
  modal.addEventListener('hidden.bs.modal', function () { document.body.classList.remove('plano-acao-aberto'); });
  var imprimir = modal.querySelector('[data-plano-imprimir]');
  if (imprimir) {
    imprimir.addEventListener('click', function () {
      document.body.classList.add('plano-acao-aberto');
      window.print();
    });
  }

  // A lista completa na mesma página: fecha o modal e rola até ela (o <details> dos alertas abre).
  Array.prototype.slice.call(modal.querySelectorAll('a[data-plano-ir]')).forEach(function (link) {
    link.addEventListener('click', function (evento) {
      var href = link.getAttribute('href') || '';
      if (href.charAt(0) !== '#') return;
      var alvo = document.getElementById(href.slice(1));
      if (!alvo || !window.bootstrap || !bootstrap.Modal) return;
      evento.preventDefault();
      var instancia = bootstrap.Modal.getInstance(modal);
      function irAteALista() {
        // O card recolhido dos alertas de cadastro (o alvo é o <summary> dele, ou o próprio <details>) abre, e o
        // foco vai ao <summary>; num título, ao próprio título.
        var foco = alvo;
        var detalhes = alvo.tagName === 'DETAILS' ? alvo : (alvo.closest ? alvo.closest('details') : null);
        if (detalhes) {
          detalhes.open = true;
          if (alvo === detalhes) foco = detalhes.querySelector('summary') || detalhes;
        }
        if (foco === alvo && !alvo.hasAttribute('tabindex') && !/^(A|BUTTON|INPUT|SELECT|TEXTAREA|SUMMARY)$/.test(alvo.tagName)) {
          alvo.setAttribute('tabindex', '-1');
        }
        alvo.scrollIntoView({ block: 'start', behavior: window.NEXO && NEXO.reduzMovimento ? 'auto' : 'smooth' });
        try { foco.focus({ preventScroll: true }); } catch (erro) { foco.focus(); }
      }
      if (!instancia) { irAteALista(); return; }
      modal.addEventListener('hidden.bs.modal', irAteALista, { once: true });
      instancia.hide();
    });
  });
})();
