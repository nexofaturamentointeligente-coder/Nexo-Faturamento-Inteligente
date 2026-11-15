/*
 * NEXO — Raio-X público (formulário de /avaliacao-inicial)
 * ============================================================
 * - Preenche os campos vazios com o que o visitante digitou no pré-cadastro da landing
 *   (static/js/landing-precadastro.js grava em sessionStorage, só nesta aba) e apaga o dado em seguida.
 * - Máscara e validação do WhatsApp, as mesmas do pré-cadastro (o servidor aplica a mesma regra).
 * - Confere no navegador a quantidade e o tamanho dos arquivos (o servidor confere de novo).
 * - Durante o envio, trava o botão e avisa que a leitura pode levar até um minuto.
 * Sem JavaScript, o formulário funciona do mesmo jeito: o servidor valida tudo.
 */
(function () {
  'use strict';

  var formulario = document.querySelector('[data-avaliacao-publica]');
  if (!formulario) return;

  var CHAVE = 'nexo.precadastro';
  var campoWhatsapp = document.getElementById('publica-whatsapp');
  var campoArquivos = document.getElementById('publica-arquivos');
  var erroArquivos = formulario.querySelector('[data-arquivos-erro]');
  var botao = formulario.querySelector('[data-enviar]');
  var enviando = formulario.querySelector('[data-enviando]');
  var maxArquivos = parseInt(formulario.getAttribute('data-max-arquivos'), 10) || 0;
  var maxBytes = parseInt(formulario.getAttribute('data-max-bytes'), 10) || 0;

  // ------------------------------------------------------------------
  // Dados do pré-cadastro (só preenche o que estiver vazio).
  // ------------------------------------------------------------------
  function preencher(id, valor) {
    var campo = document.getElementById(id);
    if (campo && !campo.value && typeof valor === 'string' && valor) campo.value = valor.slice(0, 400);
  }

  function escolher(id, rotulo) {
    var lista = document.getElementById(id);
    if (!lista || lista.value || typeof rotulo !== 'string' || !rotulo) return;
    Array.prototype.forEach.call(lista.options, function (opcao) {
      if (opcao.value && opcao.textContent.trim() === rotulo.trim()) lista.value = opcao.value;
    });
  }

  var guardado = null;
  try {
    guardado = JSON.parse(window.sessionStorage.getItem(CHAVE) || 'null');
    window.sessionStorage.removeItem(CHAVE);
  } catch (erro) { guardado = null; }
  if (guardado && typeof guardado === 'object') {
    preencher('publica-nome', guardado.nome);
    preencher('publica-empresa', guardado.empresa);
    preencher('publica-whatsapp', guardado.whatsapp);
    preencher('publica-email', guardado.email);
    preencher('publica-dificuldade', guardado.desafio);
    escolher('publica-plano', guardado.plano);
    escolher('publica-faixa', guardado.faturamento);
  }

  // ------------------------------------------------------------------
  // WhatsApp do visitante: máscara (00) 00000-0000 ou (00) 0000-0000.
  // ------------------------------------------------------------------
  // As regras de whatsapp_service.higienizar_telefone (as do envio pela Evolution), que o servidor do Raio-X
  // público também aplica (blueprints/avaliacao_publica._whatsapp). Mesmo bloco em static/js/landing-precadastro.js.
  //   - O rótulo antes do número ("Tel:", "WhatsApp:") sai. Letra, barra, vírgula ou um grupo final de 1 a 3
  //     dígitos depois do começo do número (ramal, sobra, dois números) deixam o valor ambíguo: ele fica como foi
  //     digitado, inválido. Caracteres invisíveis de formatação e traços tipográficos não contam, e o "0xx" da
  //     longa distância é o zero de tronco.
  //   - Com "+" ou "00", o DDI tem de ser o 55, com o 0 de longa distância opcional: número estrangeiro ou DDI
  //     sem DDD não passam. Sem eles, só o zero da frente sai, e 12 ou 13 dígitos ficam inválidos, em vez de o 55
  //     do começo virar DDI e o número virar outro (com 10 ou 11 dígitos, o 55 é o DDD do Rio Grande do Sul).
  //   - DDD em uso, fixo de 8 dígitos começando por 2 a 5 ou celular de 9 começando por 9, e nunca o número de
  //     exemplo (um só dígito repetido).
  var MENSAGEM_WHATSAPP = 'Informe um WhatsApp do Brasil com DDD: 10 ou 11 números.';
  var DDDS = ' 11 12 13 14 15 16 17 18 19 21 22 24 27 28 31 32 33 34 35 37 38 41 42 43 44 45 46 47 48 49 51 53 ' +
    '54 55 61 62 63 64 65 66 67 68 69 71 73 74 75 77 79 81 82 83 84 85 86 87 88 89 91 92 93 94 95 96 97 98 99 ';

  function numeroBrasileiro(numeros) {
    return /^[1-9][1-9]([2-5][0-9]{7}|9[0-9]{8})$/.test(numeros) &&
      DDDS.indexOf(' ' + numeros.slice(0, 2) + ' ') >= 0 && !/^(\d)\1+$/.test(numeros.slice(2));
  }

  // {numeros, internacional, ambiguo} do que foi digitado ou colado, a partir do primeiro "+" ou dígito. Antes, os
  // caracteres invisíveis de formatação saem (o Contatos do Mac envolve o número copiado em marcas de direção), os
  // traços tipográficos viram hífen e o "0xx" da longa distância vira o zero de tronco.
  var INVISIVEIS = /[\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]/g;
  var TRACOS = /[\u2010-\u2015\u2212\ufe58\ufe63\uff0d]/g;
  // A forma da própria máscara, mesmo editada no meio ("(11) 981234-567"): os grupos dela não são ramal.
  var FORMA_DA_MASCARA = /^\d*\)?\s?\d*-?\d*$/;

  function lerTelefone(valor) {
    var texto = String(valor || '').replace(INVISIVEIS, '').replace(TRACOS, '-').replace(/0\s*xx/gi, '0');
    var inicio = texto.search(/[+0-9]/);
    texto = inicio < 0 ? '' : texto.slice(inicio);
    var numeros = texto.replace(/[^0-9]/g, '');
    var comZeroZero = numeros.indexOf('00') === 0;
    var grupos = texto.split(/[^0-9]+/).filter(Boolean);
    // Grupo final de 1 a 3 dígitos fora da forma da máscara: ramal ou sobra ("3222-1234 21", "98917-1218 (99)").
    var grupoFinalCurto = grupos.length > 1 && grupos[grupos.length - 1].length < 4 && !FORMA_DA_MASCARA.test(texto);
    return {
      numeros: comZeroZero ? numeros.slice(2) : numeros,
      internacional: texto.charAt(0) === '+' || comZeroZero,
      ambiguo: /[^0-9\s().+\-]/.test(texto) || texto.indexOf('+', 1) >= 0 || grupoFinalCurto
    };
  }

  // DDD e número (10 ou 11 dígitos) de um WhatsApp completo e válido, ou null.
  function telefoneNacional(valor) {
    var leitura = lerTelefone(valor);
    var numeros = leitura.numeros;
    if (leitura.ambiguo) return null;
    if (leitura.internacional) {
      if (numeros.indexOf('55') !== 0) return null;
      numeros = numeros.slice(2);
    }
    if (numeros.charAt(0) === '0') numeros = numeros.slice(1);
    return numeroBrasileiro(numeros) ? numeros : null;
  }

  function mascara(numeros) {
    if (!numeros) return '';
    if (numeros.length <= 2) return '(' + numeros;
    var ddd = numeros.slice(0, 2);
    var resto = numeros.slice(2);
    var corte = resto.length > 8 ? 5 : 4;
    if (resto.length <= corte) return '(' + ddd + ') ' + resto;
    return '(' + ddd + ') ' + resto.slice(0, corte) + '-' + resto.slice(corte);
  }

  // O que o campo mostra: o número completo, na máscara; o nacional ainda incompleto, na máscara aos poucos; o
  // resto (internacional incompleto, ambíguo, só zeros ou longo demais) como foi digitado, para o visitante ver.
  function formatarTelefone(valor) {
    var nacional = telefoneNacional(valor);
    if (nacional) return mascara(nacional);
    var leitura = lerTelefone(valor);
    var numeros = leitura.numeros.charAt(0) === '0' ? leitura.numeros.slice(1) : leitura.numeros;
    if (leitura.ambiguo || leitura.internacional || /^0+$/.test(leitura.numeros) || numeros.length > 11) {
      return String(valor || '');
    }
    return mascara(numeros);
  }

  function remascarar() {
    var formatado = formatarTelefone(campoWhatsapp.value);
    if (formatado !== campoWhatsapp.value) campoWhatsapp.value = formatado;
  }

  if (campoWhatsapp) {
    campoWhatsapp.addEventListener('input', function (evento) {
      campoWhatsapp.setCustomValidity('');
      // Ao apagar, não remonta a máscara (o hífen e os parênteses voltariam a cada tecla). Com o cursor no meio,
      // também não: o valor novo jogaria o cursor para o fim e a tecla seguinte entraria fora do lugar. O blur e
      // o envio remontam.
      if (evento.inputType && evento.inputType.indexOf('delete') === 0) return;
      if (campoWhatsapp.selectionStart !== campoWhatsapp.value.length) return;
      remascarar();
    });
    campoWhatsapp.addEventListener('blur', remascarar);
  }

  // ------------------------------------------------------------------
  // Arquivos: quantidade e tamanho somado.
  // ------------------------------------------------------------------
  function conferirArquivos() {
    if (!campoArquivos) return true;
    var arquivos = campoArquivos.files || [];
    var total = 0;
    for (var i = 0; i < arquivos.length; i++) total += arquivos[i].size || 0;
    var mensagem = '';
    if (maxArquivos && arquivos.length > maxArquivos) {
      mensagem = 'Envie no máximo ' + maxArquivos + ' relatórios por vez.';
    } else if (maxBytes && total > maxBytes) {
      mensagem = 'Os relatórios passam de ' + Math.round(maxBytes / 1048576) + ' MB somados: envie menos meses por vez.';
    }
    campoArquivos.setCustomValidity(mensagem);
    if (erroArquivos) {
      erroArquivos.textContent = mensagem;
      erroArquivos.hidden = !mensagem;
    }
    return !mensagem;
  }

  if (campoArquivos) campoArquivos.addEventListener('change', conferirArquivos);

  // ------------------------------------------------------------------
  // Envio
  // ------------------------------------------------------------------
  // Destrava se o envio for interrompido (Parar ou Esc no navegador) ou se a página voltar do histórico: no erro
  // de navegação (onde houver a Navigation API) e na volta pelo histórico (pageshow persistido). Sem a Navigation
  // API, um prazo de reserva solta o botão. Ele não marca o fim do envio: o gunicorn (worker gthread) não corta o
  // pedido aos 180 s, e o upload de até 8 MB numa conexão lenta pode passar de 3 minutos; por isso o prazo só
  // existe onde não há outro jeito de saber que a navegação parou, e cada envio cancela o do anterior.
  var PRAZO_PARA_DESTRAVAR_MS = 200000;
  var temNavigationApi = !!(window.navigation && typeof window.navigation.addEventListener === 'function');
  var destravar = null;
  function liberar() {
    if (destravar !== null) {
      window.clearTimeout(destravar);
      destravar = null;
    }
    if (botao) botao.disabled = false;
    if (enviando) enviando.hidden = true;
  }
  window.addEventListener('pageshow', function (evento) { if (evento.persisted) liberar(); });
  if (temNavigationApi) window.navigation.addEventListener('navigateerror', liberar);

  formulario.addEventListener('submit', function (evento) {
    var arquivosValidos = conferirArquivos();
    if (campoWhatsapp && campoWhatsapp.value) {
      var nacional = telefoneNacional(campoWhatsapp.value);
      if (nacional) campoWhatsapp.value = mascara(nacional);
      campoWhatsapp.setCustomValidity(nacional ? '' : MENSAGEM_WHATSAPP);
    }
    if (!arquivosValidos || !formulario.checkValidity()) {
      evento.preventDefault();
      formulario.reportValidity();
      return;
    }
    if (botao) {
      // Trava depois que o navegador já montou o envio (um botão desabilitado antes sairia do formulário).
      window.setTimeout(function () { botao.disabled = true; }, 0);
      if (!temNavigationApi) {
        if (destravar !== null) window.clearTimeout(destravar);
        destravar = window.setTimeout(liberar, PRAZO_PARA_DESTRAVAR_MS);
      }
    }
    if (enviando) enviando.hidden = false;
  });
})();
