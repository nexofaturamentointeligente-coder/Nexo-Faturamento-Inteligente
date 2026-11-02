/* NEXO — pedido discreto de avaliação no painel do cliente (COM-002)
 * ------------------------------------------------------------------
 * O parcial funciona sem este arquivo ("Mais tarde" é um POST comum). Com ele:
 * - ao aparecer, avisa o servidor (pausa o pedido) e leva o foco ao diálogo;
 * - "Mais tarde" e Esc registram o adiamento e fecham, devolvendo o foco;
 * - nenhuma falha de rede atrapalha o portal: o pior caso é o pedido voltar depois.
 */
(function () {
  'use strict';

  var dialogo = document.getElementById('solicitacaoAvaliacao');
  var formAdiar = document.getElementById('solicitacaoAdiarForm');
  if (!dialogo || !formAdiar || !window.fetch) return;

  var conteudo = document.getElementById('conteudo');
  var meta = document.querySelector('meta[name="csrf-token"]');
  var anterior = null, fechado = false;

  /* keepalive: o adiamento chega ao servidor mesmo se o cliente navegar em seguida. */
  function avisar(url) {
    var cabecalhos = { 'X-Requested-With': 'fetch' };
    if (meta) cabecalhos['X-CSRFToken'] = meta.getAttribute('content');
    fetch(url, { method: 'POST', headers: cabecalhos, credentials: 'same-origin', keepalive: true })
      .catch(function () { /* falha silenciosa */ });
  }

  function focarPagina() {
    var volta = anterior && anterior !== document.body && document.contains(anterior) ? anterior : conteudo;
    if (volta) volta.focus({ preventScroll: true });
  }

  /* Fecha sem esperar a resposta: se o envio falhar, fecha do mesmo jeito. */
  function adiar() {
    if (fechado) return;
    fechado = true;
    avisar(formAdiar.action);
    dialogo.hidden = true;
    focarPagina();
  }

  function abrir() {
    anterior = document.activeElement;
    dialogo.focus({ preventScroll: true });
    avisar(dialogo.getAttribute('data-exibida-url'));
  }

  formAdiar.addEventListener('submit', function (evento) {
    evento.preventDefault();
    adiar();
  });

  /* Diálogo não modal: Esc vale com o foco nele ou solto na página. Se o foco estiver
     em outro controle ou houver outra camada aberta, o Esc é dela (menu, sino, NexoBot). */
  function outraCamadaAberta() {
    var robo = document.getElementById('nexobotPanel');
    return !!document.querySelector('.app-shell.nav-open, .dropdown-menu.show, .modal.show') || !!(robo && !robo.hidden);
  }
  document.addEventListener('keydown', function (evento) {
    if (evento.key !== 'Escape' || evento.defaultPrevented || fechado) return;
    var foco = document.activeElement;
    var focoSolto = !foco || foco === document.body || foco === conteudo;
    if (dialogo.contains(foco) || (focoSolto && !outraCamadaAberta())) {
      evento.preventDefault();
      adiar();
    }
  });

  /* Aba em segundo plano: só conta como exibido quando alguém puder ver o pedido. */
  if (document.hidden) {
    document.addEventListener('visibilitychange', function aoVer() {
      if (document.hidden) return;
      document.removeEventListener('visibilitychange', aoVer);
      abrir();
    });
  } else {
    abrir();
  }
})();
