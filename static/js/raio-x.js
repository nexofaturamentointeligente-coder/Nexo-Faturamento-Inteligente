/* Raio-X do PDV e Auditoria de custos, para quem não é do ramo: a explicação (i) de cada
   indicador e as tabelas com as 5 linhas de maior impacto, busca e filtro rápido.
   Só lê o HTML que o servidor montou: nenhum valor que o plano esconde chega aos atributos
   (raio_x_servico.visao troca por None antes do template). Sem JavaScript, tudo continua
   legível: a explicação abre pelo <details> e a tabela mostra todas as linhas. */
(function () {
  'use strict';

  /* ---- Explicação (i) dos indicadores ----
     <details data-kpi-info>: abre uma de cada vez; fecha com Esc (o foco volta ao botão) e com
     um clique fora dela. Com data-kpi-info-passar (o (i) curto da Auditoria), num aparelho com mouse,
     abre também ao passar o ponteiro e fecha pouco depois de ele sair (dá tempo de levar o ponteiro
     do botão ao balão, que é parte do <details>); o clique, então, fixa o balão aberto, e o próximo
     clique fecha. No toque e no teclado, nada muda. */
  var infos = Array.prototype.slice.call(document.querySelectorAll('details[data-kpi-info]'));
  var comMouse = window.matchMedia ? window.matchMedia('(hover: hover) and (pointer: fine)') : null;
  function cancelarFechamento(info) {
    if (info._fechar) { window.clearTimeout(info._fechar); info._fechar = null; }
  }
  infos.forEach(function (info) {
    if (!info.hasAttribute('data-kpi-info-passar')) return;
    var botao = info.querySelector('summary');
    info._porPassar = false;
    info.addEventListener('mouseenter', function () {
      cancelarFechamento(info);
      if (!comMouse || !comMouse.matches || info.open) return;
      info._porPassar = true;
      info.open = true;
    });
    info.addEventListener('mouseleave', function () {
      if (!info._porPassar) return;
      cancelarFechamento(info);
      info._fechar = window.setTimeout(function () {
        info._fechar = null;
        if (!info._porPassar) return;
        info._porPassar = false;
        info.open = false;
      }, 200);
    });
    if (botao) {
      botao.addEventListener('click', function (evento) {
        // Aberto pelo ponteiro: o clique fixa (não fecha o que a pessoa acabou de ver abrir).
        if (!info._porPassar) return;
        evento.preventDefault();
        cancelarFechamento(info);
        info._porPassar = false;
      });
    }
  });
  function fecharInfos(exceto) {
    infos.forEach(function (info) {
      if (info !== exceto && info.open) info.open = false;
    });
  }
  infos.forEach(function (info) {
    info.addEventListener('toggle', function () {
      if (info.open) fecharInfos(info);
      else info._porPassar = false;
    });
  });
  document.addEventListener('keydown', function (evento) {
    if (evento.key !== 'Escape') return;
    infos.forEach(function (info) {
      if (!info.open) return;
      info.open = false;
      var botao = info.querySelector('summary');
      if (botao && info.contains(document.activeElement)) botao.focus();
    });
  });
  document.addEventListener('click', function (evento) {
    infos.forEach(function (info) {
      if (info.open && !info.contains(evento.target)) info.open = false;
    });
  });

  // A base (templates/base.html) decide entre rolagem e cartões pela largura de cada tabela; uma tabela dentro de
  // um <details> fechado (os alertas de cadastro da Auditoria) é medida com largura zero: ao abrir, refaz a conta.
  function reajustarLayout() {
    if (window.NEXO && typeof window.NEXO.ajustarLayout === 'function') window.NEXO.ajustarLayout();
  }
  document.querySelectorAll('details').forEach(function (recolhido) {
    if (!recolhido.querySelector('.table-responsive-x')) return;
    recolhido.addEventListener('toggle', function () {
      if (recolhido.open) reajustarLayout();
    });
  });

  /* ---- Tabelas com as 5 de maior impacto, busca e filtro rápido ----
     [data-tabela-rapida] envolve a tabela. As linhas já chegam na ordem de maior impacto
     (ou trazem data-pos-impacto e data-pos-recentes, quando há a escolha da ordem);
     data-texto é o que a busca procura e data-marcas, as marcas do filtro rápido. */
  function normalizar(texto) {
    return String(texto || '').normalize('NFD').replace(/[̀-ͯ]/g, '')
      .toLowerCase().replace(/\s+/g, ' ').trim();
  }

  document.querySelectorAll('[data-tabela-rapida]').forEach(function (bloco) {
    var corpo = bloco.querySelector('tbody');
    if (!corpo || !corpo.rows.length) return;
    var linhas = Array.prototype.slice.call(corpo.rows);
    var limite = parseInt(bloco.getAttribute('data-limite') || '5', 10) || 5;
    // Tabela curta (até o limite) fica como está: sem barra, sem botão.
    if (linhas.length <= limite) return;
    var singular = bloco.getAttribute('data-singular') || 'item';
    var plural = bloco.getAttribute('data-plural') || 'itens';
    var ferramentas = bloco.querySelector('[data-tabela-ferramentas]');
    var busca = bloco.querySelector('[data-tabela-busca]');
    var filtro = bloco.querySelector('[data-tabela-filtro]');
    var ordem = bloco.querySelector('[data-tabela-ordem]');
    var rodape = bloco.querySelector('[data-tabela-rodape]');
    var botao = bloco.querySelector('[data-tabela-mais]');
    var contagem = bloco.querySelector('[data-tabela-contagem]');
    var legenda = bloco.querySelector('caption[data-legenda-base]');
    var expandido = false;
    // Lista que o motor cortou (raio_x.LIMITE_ITENS_LISTA): o botão não diz "todos", a nota acima diz de quantos.
    var cortada = bloco.hasAttribute('data-cortada');

    linhas.forEach(function (linha, posicao) {
      linha._texto = normalizar(linha.getAttribute('data-texto') || (linha.cells[0] && linha.cells[0].textContent));
      linha._marcas = (linha.getAttribute('data-marcas') || '').split(/\s+/).filter(Boolean);
      linha._original = posicao;
    });

    function nomeDe(total) {
      return total === 1 ? singular : plural;
    }

    function ordenar() {
      // A ordem vem da opção do filtro, quando ela traz uma (data-ordem: a margem baixa e a negativa começam pelo
      // maior prejuízo); senão, da escolha da ordem; sem as duas, é a do servidor. Cada ordem tem a posição da linha
      // em data-pos-<ordem>.
      var opcao = filtro ? filtro.options[filtro.selectedIndex] : null;
      var pelaOpcao = opcao ? opcao.getAttribute('data-ordem') || '' : '';
      if (!/^[a-z]+$/.test(pelaOpcao)) pelaOpcao = '';
      var chave = pelaOpcao || (ordem ? (/^[a-z]+$/.test(ordem.value) ? ordem.value : 'impacto') : '');
      var atributo = chave ? 'data-pos-' + chave : '';
      linhas.sort(function (a, b) {
        if (!atributo) return a._original - b._original;
        var pa = parseInt(a.getAttribute(atributo), 10);
        var pb = parseInt(b.getAttribute(atributo), 10);
        if (isNaN(pa) || isNaN(pb)) return a._original - b._original;
        return pa - pb;
      });
      linhas.forEach(function (linha) { corpo.appendChild(linha); });
      // O nome acessível da tabela diz a ordem em uso.
      if (legenda) {
        var sufixo = '';
        if (pelaOpcao) {
          sufixo = opcao.getAttribute('data-legenda') || '';
        } else if (ordem) {
          var escolhida = ordem.options[ordem.selectedIndex];
          sufixo = escolhida ? escolhida.text.toLowerCase() : '';
        } else {
          sufixo = legenda.getAttribute('data-legenda-padrao') || '';
        }
        legenda.textContent = legenda.getAttribute('data-legenda-base') + (sufixo ? ', ' + sufixo : '');
      }
    }

    // A base (templates/base.html) decide entre rolagem e cartões pela largura da tabela e põe a dica e o foco de
    // rolagem: as linhas que aparecem ou somem mudam essa largura.
    function reajustar() {
      reajustarLayout();
    }

    function aplicar() {
      var termo = busca ? normalizar(busca.value) : '';
      var marca = filtro ? filtro.value : '';
      var encontradas = linhas.filter(function (linha) {
        if (termo && linha._texto.indexOf(termo) === -1) return false;
        if (marca && linha._marcas.indexOf(marca) === -1) return false;
        return true;
      });
      var mostrar = expandido ? encontradas.length : Math.min(limite, encontradas.length);
      linhas.forEach(function (linha) { linha.hidden = true; linha.classList.remove('is-listra'); });
      // A listra alterna pelas linhas visíveis (o nth-child da tabela contaria as escondidas).
      encontradas.slice(0, mostrar).forEach(function (linha, posicao) {
        linha.hidden = false;
        linha.classList.toggle('is-listra', posicao % 2 === 1);
      });

      var filtrado = Boolean(termo || marca);
      if (botao) {
        var sobra = encontradas.length > limite;
        botao.hidden = !sobra;
        botao.setAttribute('aria-expanded', expandido && sobra ? 'true' : 'false');
        // O botão só aparece com mais linhas do que o limite: o número é sempre plural.
        var rotulo = botao.querySelector('[data-tabela-mais-rotulo]') || botao;
        rotulo.textContent = expandido && sobra
          ? 'Mostrar menos (top ' + limite + ')'
          : (cortada && !(busca && busca.value) && !(filtro && filtro.value)
            ? 'Ver os ' + encontradas.length + ' ' + plural + ' da lista'
            : 'Ver todos os ' + encontradas.length + ' ' + plural);
        botao.classList.toggle('is-aberto', expandido && sobra);
      }
      reajustar();
      if (contagem) {
        var total = linhas.length;
        if (!encontradas.length) {
          contagem.textContent = 'Nenhum ' + singular + ' atende à busca ou ao filtro. Mude a busca ou escolha outro filtro.';
        } else if (filtrado) {
          contagem.textContent = encontradas.length + ' de ' + total + ' ' + nomeDe(total) + ' no filtro'
            + (mostrar < encontradas.length ? '; mostrando ' + mostrar : '') + '.';
        } else {
          contagem.textContent = 'Mostrando ' + mostrar + ' de ' + total + ' ' + nomeDe(total) + '.';
        }
      }
    }

    if (busca) busca.addEventListener('input', aplicar);
    if (busca) busca.addEventListener('search', aplicar);
    if (filtro) filtro.addEventListener('change', function () { ordenar(); aplicar(); });
    if (ordem) ordem.addEventListener('change', function () { ordenar(); aplicar(); });
    if (botao) {
      botao.addEventListener('click', function () {
        expandido = !expandido;
        aplicar();
        // Ao recolher uma lista longa, a página não fica lá embaixo: o botão volta à vista (com o foco nele).
        if (!expandido && botao.scrollIntoView) botao.scrollIntoView({ block: 'nearest' });
      });
    }

    bloco.setAttribute('data-pronta', '');
    if (ferramentas) ferramentas.hidden = false;
    if (rodape) rodape.hidden = false;
    ordenar();
    aplicar();
    // A contagem só passa a ser anunciada depois da primeira montagem (nada é lido ao abrir a página).
    if (contagem) contagem.setAttribute('aria-live', 'polite');
  });
})();
