/*
 * NEXO — Regra da rolagem no aceite dos Termos de Uso e da Política de Privacidade
 * ============================================================================
 * Pedido do dono (16/11/2026): a caixa "Li e concordo" e o botão do aceite nascem disabled no HTML e só
 * liberam quando a pessoa rola CADA texto até o fim, isto é, quando
 * scrollTop + clientHeight >= scrollHeight - 2 (a folga cobre o zoom e as medidas fracionárias).
 *   - Texto curto, que cabe sem rolar, libera na hora.
 *   - A janela redimensionada, ou o <details> que se abre, é conferida de novo.
 *   - End, PageDown e as setas no texto em foco rolam como a roda do mouse e contam do mesmo jeito.
 *   - Reforço: um IntersectionObserver no marcador do fim do texto (zoom, leitor de tela).
 *   - Um texto lido continua lido se a pessoa voltar a subir.
 * A primeira conferência espera as fontes da página: com a fonte de reserva, um texto curto poderia
 * caber na caixa e deixar de caber depois.
 *
 * Ligação automática por atributos, sem JavaScript inline (CSP):
 *   [data-aceite-rolagem]  o grupo; o resto fica dentro dele
 *   [data-aceite-texto]    cada contêiner de texto (overflow-y: scroll), com data-aceite-nome
 *   [data-aceite-fim]      o marcador do fim, último filho do contêiner (opcional)
 *   [data-aceite-libera]   o que nasce disabled e libera depois de todos os textos (a caixa e o botão)
 *   [data-aceite-aviso]    o aviso em aria-live; data-pronto traz o texto de quando tudo foi lido
 * O grupo liberado ganha data-aceite-liberado="1"; cada texto lido, data-aceite-lido="1".
 *
 * Reuso (por exemplo, no aceite da landing): window.NexoAceiteRolagem.observar(conteiner, aoChegarAoFim)
 * chama aoChegarAoFim(conteiner) uma única vez, no fim da rolagem (ou na hora, se não houver rolagem), e
 * devolve a função que desliga a observação. NexoAceiteRolagem.ligar(raiz) liga os grupos de um trecho
 * desenhado depois do carregamento da página; cada grupo é ligado uma única vez (data-aceite-ligado), então
 * um texto redesenhado pede um grupo novo, ou observar() direto no contêiner.
 *
 * A rolagem não é prova: o servidor continua exigindo confirmo=1, e a prova do aceite é a versão, o
 * instante e o IP gravados. Sem JavaScript nada libera, e o <noscript> da página explica o motivo.
 */
(function () {
  'use strict';

  if (window.NexoAceiteRolagem) return;  // arquivo incluído duas vezes: a primeira cópia já ligou a página

  var FOLGA_PX = 2;
  var ESPERA_DAS_FONTES_MS = 3000;
  var TECLAS_DE_ROLAGEM = /^(End|PageDown|ArrowDown|Down| |Spacebar)$/;

  function chegouAoFim(conteiner) {
    return conteiner.scrollTop + conteiner.clientHeight >= conteiner.scrollHeight - FOLGA_PX;
  }

  // Fechado num <details> (ou com display: none), o contêiner mede zero e não conta como lido.
  function desenhado(conteiner) {
    return conteiner.isConnected !== false && conteiner.clientHeight > 0 && conteiner.getClientRects().length > 0;
  }

  // Uma única vez, quando as fontes da página terminarem de carregar (ou depois do prazo, se não terminarem).
  function depoisDasFontes(funcao) {
    var feito = false;
    function uma() {
      if (feito) return;
      feito = true;
      funcao();
    }
    var fontes = document.fonts;
    if (fontes && fontes.ready && typeof fontes.ready.then === 'function') {
      fontes.ready.then(uma, uma);
      window.setTimeout(uma, ESPERA_DAS_FONTES_MS);
    } else {
      uma();
    }
  }

  function observar(conteiner, aoChegarAoFim) {
    var ativo = true;
    var pronto = false;
    var quadro = 0;
    var tamanho = null;
    var interseccao = null;
    var marcador = conteiner.querySelector('[data-aceite-fim]');

    function lido() {
      return desenhado(conteiner) && chegouAoFim(conteiner);
    }

    function concluir() {
      if (!ativo) return;
      desligar();
      if (typeof aoChegarAoFim === 'function') aoChegarAoFim(conteiner);
    }

    // A rolagem vale na hora: a pessoa está diante do texto que acabou de rolar.
    function aoRolar() {
      if (ativo && lido()) concluir();
    }

    function conferir() {
      quadro = 0;
      if (ativo && pronto && lido()) concluir();
    }

    function agendar() {
      if (ativo && !quadro) quadro = window.requestAnimationFrame(conferir);
    }

    // O navegador rola o contêiner em foco com o teclado (e o scroll chega acima); esta conferência
    // cobre a tecla apertada com o texto já parado no fim.
    function aoTeclar(evento) {
      if (TECLAS_DE_ROLAGEM.test(evento.key || '')) agendar();
    }

    function desligar() {
      if (!ativo) return;
      ativo = false;
      conteiner.removeEventListener('scroll', aoRolar);
      conteiner.removeEventListener('keydown', aoTeclar);
      window.removeEventListener('resize', agendar);
      document.removeEventListener('toggle', agendar, true);
      if (quadro) window.cancelAnimationFrame(quadro);
      if (tamanho) tamanho.disconnect();
      if (interseccao) interseccao.disconnect();
    }

    conteiner.addEventListener('scroll', aoRolar, { passive: true });
    conteiner.addEventListener('keydown', aoTeclar);
    window.addEventListener('resize', agendar);
    // O <details> aberto passa a desenhar o texto sem mudar o tamanho da janela. O evento toggle não
    // sobe pela árvore: a escuta fica na captura.
    document.addEventListener('toggle', agendar, true);

    depoisDasFontes(function () {
      if (!ativo) return;
      pronto = true;
      if (typeof window.ResizeObserver === 'function') {
        tamanho = new window.ResizeObserver(agendar);
        tamanho.observe(conteiner);
      }
      if (marcador && typeof window.IntersectionObserver === 'function') {
        interseccao = new window.IntersectionObserver(function (entradas) {
          for (var i = 0; i < entradas.length; i++) {
            if (entradas[i].isIntersecting && desenhado(conteiner)) {
              concluir();
              return;
            }
          }
        }, { root: conteiner, threshold: 0 });
        interseccao.observe(marcador);
      }
      conferir();
    });

    return desligar;
  }

  function nomeDoTexto(texto) {
    return texto.getAttribute('data-aceite-nome') || 'o texto';
  }

  // "A", "A e B", "A, B e C".
  function listar(nomes) {
    if (nomes.length < 2) return nomes.join('');
    return nomes.slice(0, -1).join(', ') + ' e ' + nomes[nomes.length - 1];
  }

  function ligarGrupo(grupo) {
    if (grupo.getAttribute('data-aceite-ligado') === '1') return;
    grupo.setAttribute('data-aceite-ligado', '1');
    var textos = Array.prototype.slice.call(grupo.querySelectorAll('[data-aceite-texto]'));
    var liberaveis = grupo.querySelectorAll('[data-aceite-libera]');
    var aviso = grupo.querySelector('[data-aceite-aviso]');
    var faltam = textos.slice();

    // O texto do servidor vai por textContent, nunca como HTML.
    function avisar(mensagem) {
      if (aviso && mensagem && aviso.textContent !== mensagem) aviso.textContent = mensagem;
    }

    function liberar() {
      for (var i = 0; i < liberaveis.length; i++) liberaveis[i].disabled = false;
      grupo.setAttribute('data-aceite-liberado', '1');
      avisar(aviso && aviso.getAttribute('data-pronto'));
    }

    if (!textos.length) {
      liberar();  // nada para ler: o grupo não prende ninguém
      return;
    }
    textos.forEach(function (texto) {
      observar(texto, function () {
        texto.setAttribute('data-aceite-lido', '1');
        faltam = faltam.filter(function (outro) { return outro !== texto; });
        if (faltam.length) {
          avisar('Falta rolar até o fim: ' + listar(faltam.map(nomeDoTexto)) + '.');
        } else {
          liberar();
        }
      });
    });
  }

  function ligar(raiz) {
    var base = raiz || document;
    if (base.matches && base.matches('[data-aceite-rolagem]')) ligarGrupo(base);
    var grupos = base.querySelectorAll('[data-aceite-rolagem]');
    for (var i = 0; i < grupos.length; i++) ligarGrupo(grupos[i]);
  }

  window.NexoAceiteRolagem = { observar: observar, ligar: ligar, chegouAoFim: chegouAoFim };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { ligar(document); });
  } else {
    ligar(document);
  }
})();
