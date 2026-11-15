/*
 * NEXO — Pré-cadastro comercial da landing (decisões do dono, 15/11/2026)
 * ============================================================
 * O CONTRATAR da seção #planos é um link wa.me real, desenhado pelo servidor (macro
 * link_whatsapp, número de config.WHATSAPP_EMPRESA_NUMERO). Este script o transforma no
 * botão que abre o diálogo #precadastro:
 *   Etapa 1  aceite dos termos ....... DESLIGADA (ETAPA_TERMOS_ATIVA = false; ver landing.html)
 *   Etapa 2  formulário de qualificação (nada vai ao servidor)
 *   Etapa 3  Raio-X: realizar (página da avaliação inicial), pedir ajuda com os relatórios ou
 *            finalizar sem ele; os dois últimos abrem o WhatsApp da equipe com a mensagem abaixo.
 *
 * Sem o diálogo (número ausente ou inválido), sem suporte a <dialog> ou sem JavaScript, nada
 * muda: o CONTRATAR continua levando direto ao WhatsApp com a mensagem de contratação.
 * O número vem só do atributo data-numero, desenhado pelo servidor; ele nunca é fixo aqui.
 *
 * Exceção registrada à regra "a mensagem pré-preenchida nunca leva dado do cliente"
 * (Docs/WHATSAPP_EVOLUTION.md): aqui a mensagem leva só o que o próprio visitante digitou, e ele
 * a confere no WhatsApp antes de enviar. Arquivo separado do landing.js de propósito: uma falha
 * aqui não derruba o menu, o carrossel de depoimentos nem os gráficos.
 */
(function () {
  'use strict';

  // ETAPA 1 (aceite dos Termos de Uso e da Política de Privacidade). Mude para true só depois
  // dos passos do comentário da Etapa 1 em templates/landing.html: textos publicados, páginas
  // públicas nos links e decisão sobre o registro do aceite no servidor.
  var ETAPA_TERMOS_ATIVA = false;

  // Leva os dados do formulário à página da avaliação inicial (mesma aba, só neste navegador).
  var CHAVE_RAIO_X = 'nexo.precadastro';

  var dialogo = document.getElementById('precadastro');
  var gatilho = document.querySelector('[data-precadastro-gatilho] a[href^="https://wa.me/"]');
  if (!dialogo || !gatilho || typeof dialogo.showModal !== 'function') return;
  var numero = dialogo.getAttribute('data-numero') || '';
  if (!/^55\d{10,11}$/.test(numero)) return;

  function porId(id) { return document.getElementById(id); }

  var etapas = { 1: porId('step-1'), 2: porId('step-2'), 3: porId('step-3') };
  var formulario = porId('form-pre-cadastro');
  var campoWhatsapp = porId('lead-whatsapp');
  var aceiteTermos = porId('check-termos');
  var aceitePrivacidade = porId('check-privacidade');
  var continuar = porId('btn-next-1');
  var botaoRaioX = dialogo.querySelector('[data-raiox-destino]');
  var saidasWhatsapp = dialogo.querySelectorAll('[data-raiox-situacao]');
  if (!etapas[1] || !etapas[2] || !etapas[3] || !formulario || !campoWhatsapp) return;

  var ETAPA_INICIAL = ETAPA_TERMOS_ATIVA ? 1 : 2;
  var SITUACOES = {
    ajuda: { raioX: 'Não realizado', auxilio: 'Sim, cliente solicitou auxílio com relatórios' },
    pular: { raioX: 'Não solicitado', auxilio: 'Não' }
  };
  var dados = null;

  // ------------------------------------------------------------------
  // Texto: uma linha (sem controle nem quebra) ou bloco curto (quebras simples preservadas).
  // ------------------------------------------------------------------
  function umaLinha(valor, limite) {
    return String(valor || '').replace(/[\u0000-\u001f\u007f-\u009f\u2028\u2029]+/g, ' ')
      .replace(/\s+/g, ' ').trim().slice(0, limite);
  }

  function bloco(valor, limite) {
    return String(valor || '').replace(/\r\n?/g, '\n')
      .replace(/[\u0000-\u0009\u000b-\u001f\u007f-\u009f\u2028\u2029]+/g, ' ')
      .replace(/[ \t]+/g, ' ').replace(/ *\n */g, '\n').replace(/\n{3,}/g, '\n\n')
      .trim().slice(0, limite);
  }

  // ------------------------------------------------------------------
  // WhatsApp do visitante: máscara (00) 00000-0000 ou (00) 0000-0000.
  // ------------------------------------------------------------------
  // As regras de whatsapp_service.higienizar_telefone (as do envio pela Evolution), que o servidor do Raio-X
  // público também aplica (blueprints/avaliacao_publica._whatsapp). Mesmo bloco em static/js/avaliacao-publica.js.
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

  campoWhatsapp.addEventListener('input', function (evento) {
    campoWhatsapp.setCustomValidity('');
    // Ao apagar, não remonta a máscara (o hífen e os parênteses voltariam a cada tecla). Com o cursor no meio,
    // também não: o valor novo jogaria o cursor para o fim e a tecla seguinte entraria fora do lugar. O blur e o
    // envio remontam.
    if (evento.inputType && evento.inputType.indexOf('delete') === 0) return;
    if (campoWhatsapp.selectionStart !== campoWhatsapp.value.length) return;
    remascarar();
  });
  campoWhatsapp.addEventListener('blur', remascarar);

  // ------------------------------------------------------------------
  // Mensagem comercial (formato definido pelo dono) e o link wa.me da equipe NEXO.
  // ------------------------------------------------------------------
  function mensagem(situacao) {
    var linhas = [
      '*NOVO CONTATO COMERCIAL | NEXO*',
      '',
      'Nome: ' + dados.nome,
      'Empresa: ' + dados.empresa,
      'WhatsApp: ' + dados.whatsapp,
      'Plano de interesse: ' + dados.plano
    ];
    if (dados.email) linhas.push('E-mail: ' + dados.email);
    if (dados.faturamento) linhas.push('Faixa de faturamento: ' + dados.faturamento);
    if (dados.desafio) linhas.push('Principal necessidade: ' + dados.desafio);
    linhas.push('', 'Situação do Raio-X: ' + situacao.raioX, 'Solicitou auxílio: ' + situacao.auxilio,
      'Origem: Landing Page da NEXO');
    return linhas.join('\n');
  }

  function linkWhatsapp(texto) {
    return 'https://wa.me/' + numero + '?text=' + encodeURIComponent(texto);
  }

  // ------------------------------------------------------------------
  // Etapas
  // ------------------------------------------------------------------
  function mostrar(etapa, foco) {
    Object.keys(etapas).forEach(function (chave) {
      etapas[chave].classList.toggle('hidden', Number(chave) !== etapa);
    });
    dialogo.setAttribute('aria-labelledby', 'precadastro-titulo-' + etapa);
    var alvo = foco || etapas[etapa].querySelector('.precadastro-titulo');
    if (alvo) {
      try { alvo.focus({ preventScroll: true }); } catch (erro) { alvo.focus(); }
    }
  }

  function prepararSaidas() {
    Array.prototype.forEach.call(saidasWhatsapp, function (saida) {
      var situacao = SITUACOES[saida.getAttribute('data-raiox-situacao')];
      if (situacao) saida.setAttribute('href', linkWhatsapp(mensagem(situacao)));
    });
  }

  // ------------------------------------------------------------------
  // Abrir e fechar
  // ------------------------------------------------------------------
  function abrir() {
    if (dialogo.open) return;
    if (ETAPA_TERMOS_ATIVA) {
      aceiteTermos.checked = false;
      aceitePrivacidade.checked = false;
      continuar.disabled = true;
    }
    dialogo.showModal();
    document.documentElement.classList.add('precadastro-aberto');
    mostrar(ETAPA_INICIAL, ETAPA_INICIAL === 2 ? porId('lead-nome') : null);
  }

  function fechar() {
    if (dialogo.open) dialogo.close();
  }

  dialogo.addEventListener('close', function () {
    document.documentElement.classList.remove('precadastro-aberto');
    try { gatilho.focus({ preventScroll: true }); } catch (erro) { gatilho.focus(); }
  });

  // Clique no fundo escurecido (fora da caixa) fecha, como o Esc e o botão de fechar. Só quando o clique
  // COMEÇOU no fundo: selecionar um texto e soltar o mouse fora da caixa não fecha.
  var cliqueComecouNoFundo = false;
  dialogo.addEventListener('pointerdown', function (evento) {
    cliqueComecouNoFundo = evento.target === dialogo;
  });
  dialogo.addEventListener('click', function (evento) {
    if (evento.target === dialogo && cliqueComecouNoFundo) fechar();
    cliqueComecouNoFundo = false;
  });
  Array.prototype.forEach.call(dialogo.querySelectorAll('[data-precadastro-fechar]'), function (botao) {
    botao.addEventListener('click', fechar);
  });

  // Etapa 1: as duas caixas liberam o Continuar.
  function conferirAceites() {
    continuar.disabled = !(aceiteTermos.checked && aceitePrivacidade.checked);
  }
  aceiteTermos.addEventListener('change', conferirAceites);
  aceitePrivacidade.addEventListener('change', conferirAceites);
  continuar.addEventListener('click', function () {
    if (!continuar.disabled) mostrar(2, porId('lead-nome'));
  });

  // Nome e empresa só com espaços passariam no required: o envio confere depois de aparar.
  var camposDeTexto = [porId('lead-nome'), porId('lead-empresa')].filter(Boolean);
  camposDeTexto.forEach(function (campo) {
    campo.addEventListener('input', function () { campo.setCustomValidity(''); });
  });

  // Etapa 2: validação nativa do navegador mais a do WhatsApp; nada é enviado ao servidor.
  formulario.addEventListener('submit', function (evento) {
    evento.preventDefault();
    camposDeTexto.forEach(function (campo) {
      campo.setCustomValidity(campo.value && !umaLinha(campo.value, 120) ? 'Preencha este campo.' : '');
    });
    var nacional = telefoneNacional(campoWhatsapp.value);
    if (nacional) campoWhatsapp.value = mascara(nacional);
    campoWhatsapp.setCustomValidity(nacional ? '' : MENSAGEM_WHATSAPP);
    if (!formulario.reportValidity()) return;
    var campos = formulario.elements;
    dados = {
      nome: umaLinha(campos.nome.value, 120),
      empresa: umaLinha(campos.empresa.value, 120),
      whatsapp: mascara(nacional),
      plano: umaLinha(campos.plano.value, 40),
      email: umaLinha(campos.email.value, 254),
      faturamento: umaLinha(campos.faturamento.value, 40),
      desafio: bloco(campos.desafio.value, 400)
    };
    prepararSaidas();
    mostrar(3);
  });

  // Etapa 3: as saídas pelo WhatsApp seguem o próprio link (nova aba); o diálogo fecha em seguida.
  Array.prototype.forEach.call(saidasWhatsapp, function (saida) {
    saida.addEventListener('click', function () {
      if (!saida.getAttribute('href')) return;
      window.setTimeout(function () {
        formulario.reset();
        dados = null;
        fechar();
      }, 0);
    });
  });

  // Realizar o Raio-X: a página da avaliação inicial recebe os dados por sessionStorage
  // (só nesta aba, nunca pela URL). Sem o destino desenhado pelo servidor, o botão some.
  if (botaoRaioX) {
    if (!botaoRaioX.getAttribute('href')) {
      botaoRaioX.hidden = true;
    } else {
      botaoRaioX.addEventListener('click', function () {
        try {
          window.sessionStorage.setItem(CHAVE_RAIO_X, JSON.stringify(dados || {}));
        } catch (erro) { /* sem armazenamento: a página pede os dados de novo */ }
      });
    }
  }

  // ------------------------------------------------------------------
  // O CONTRATAR passa a abrir o diálogo (o href wa.me fica como reserva).
  // ------------------------------------------------------------------
  gatilho.setAttribute('role', 'button');
  gatilho.setAttribute('aria-haspopup', 'dialog');
  var dicaGatilho = gatilho.querySelector('.visually-hidden');
  if (dicaGatilho) dicaGatilho.textContent = ' (abre o pré-cadastro)';

  gatilho.addEventListener('click', function (evento) {
    if (evento.button !== 0 || evento.metaKey || evento.ctrlKey || evento.shiftKey || evento.altKey) return;
    evento.preventDefault();
    try {
      abrir();
    } catch (erro) {
      window.open(gatilho.href, '_blank', 'noopener');
    }
  });
  gatilho.addEventListener('keydown', function (evento) {
    if (evento.key === ' ' || evento.key === 'Spacebar') {
      evento.preventDefault();
      gatilho.click();
    }
  });
})();
