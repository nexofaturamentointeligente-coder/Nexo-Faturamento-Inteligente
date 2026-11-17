/*
 * NEXO — Captação de leads da landing (decisões do dono, 16/11/2026)
 * ============================================================
 * Os dois diálogos da landing gravam o lead no microsserviço do Raio-X: POST multipart em
 * <MICROSSERVICO_RAIOX_URL>/api/leads/raio-x, sem credenciais e sem Content-Type manual (o navegador põe o
 * boundary). A URL vem do servidor (atributo data-servico do diálogo), e a origem dela está no connect-src da
 * CSP da landing (seguranca_http.py).
 *   - #cadastro-lead (lead frio, "Cadastre-se" do topo): controlado no fim deste arquivo. Termos, dados, a
 *     Avaliação de Cenário Externo (opcional: com relatórios é o interesse 1; "só contato" é o 3) e o resultado.
 *   - #precadastro (CONTRATAR, só com o WhatsApp comercial configurado): static/js/landing-precadastro.js, que
 *     usa as peças daqui por window.NexoCaptacao. Este arquivo vem antes dele na página.
 *
 * Peças compartilhadas (window.NexoCaptacao):
 *   - texto numa linha e telefone do Brasil, com a máscara e as mesmas regras do servidor;
 *   - aceite dos documentos legais vigentes (GET /documentos-legais/vigentes, da própria origem) com a regra da
 *     rolagem: cada "Li e aceito" nasce desabilitado e só libera quando o texto foi rolado até o fim
 *     (scrollTop + clientHeight >= scrollHeight - 2) ou na hora, se o texto cabe sem rolagem. Sem os dois
 *     documentos publicados, nada vai ao serviço;
 *   - os relatórios (.csv e .xlsx; .xlsm recusado), até 12 e 15 MB somados, nenhum vazio: conferidos aqui antes
 *     do envio, porque o serviço conta toda tentativa no limite de 5 envios por hora por IP;
 *   - o envio, com prazo de 90 s, e a leitura de cada resposta do serviço (201, 400, 409, 413, 415, 429, 503 e
 *     erro de rede ou de CORS).
 * Tudo o que vem de fora (documentos, protocolo, nomes e motivos dos arquivos, mensagens do serviço) entra por
 * textContent. Sem número de WhatsApp na página, nada aqui menciona nem abre o WhatsApp. Arquivo separado do
 * landing.js de propósito: uma falha aqui não derruba o menu, o carrossel nem os gráficos.
 */
(function () {
  'use strict';

  var ROTA_DO_LEAD = '/api/leads/raio-x';
  var ROTA_DOS_DOCUMENTOS = '/documentos-legais/vigentes';
  var PRAZO_DO_ENVIO = 90000;
  var MAXIMO_DE_ARQUIVOS = 12;
  var MAXIMO_DE_BYTES = 15 * 1024 * 1024;
  var NOME_MAXIMO = 120;
  var EMAIL_MAXIMO = 254;
  var EXTENSOES_ACEITAS = ['csv', 'xlsx'];
  var EXTENSOES_COM_MACRO = ['xlsm', 'xltm', 'xlam'];
  var DOCUMENTOS = { TERMOS_USO: 'termos', POLITICA_PRIVACIDADE: 'privacidade' };
  var NUMERO_VALIDO = /^55\d{10,11}$/;

  var MENSAGEM_INDISPONIVEL = 'O serviço de cadastro está indisponível no momento. Tente de novo em instantes.';
  var MENSAGEM_REDE = 'Não foi possível falar com o serviço de cadastro agora. Confira a sua conexão e tente de ' +
    'novo em instantes.';
  var MENSAGEM_TEMPO = 'O envio demorou mais que o esperado e foi interrompido. Ele pode ter chegado: antes de ' +
    'tentar de novo, fale com a equipe NEXO.';
  var MENSAGEM_TERMOS_ATUALIZADOS = 'Os termos foram atualizados enquanto você lia. Leia a versão atual e ' +
    'confirme de novo.';
  var MENSAGEM_SEM_TERMOS = 'Os Termos de Uso e a Política de Privacidade ainda não foram publicados. Por ' +
    'enquanto, não é possível concluir o cadastro pelo site.';
  var MENSAGEM_TERMOS_FORA = 'Não foi possível carregar os Termos de Uso e a Política de Privacidade agora. ' +
    'Tente de novo em instantes.';

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

  function focar(elemento) {
    if (!elemento) return;
    try { elemento.focus({ preventScroll: true }); } catch (erro) { elemento.focus(); }
  }

  // ------------------------------------------------------------------
  // Telefone do Brasil: máscara (00) 00000-0000 ou (00) 0000-0000.
  // ------------------------------------------------------------------
  // As regras de whatsapp_service.higienizar_telefone (as do envio pela Evolution), que o microsserviço
  // (microservico_raiox/validacao.py) e o servidor do Raio-X público também aplicam. Mesmo bloco em
  // static/js/avaliacao-publica.js.
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
  var MENSAGEM_TELEFONE = 'Informe um telefone do Brasil com DDD: 10 ou 11 números.';
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

  // DDD e número (10 ou 11 dígitos) de um telefone completo e válido, ou null.
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

  function instalarMascara(campo) {
    function remascarar() {
      var formatado = formatarTelefone(campo.value);
      if (formatado !== campo.value) campo.value = formatado;
    }
    campo.addEventListener('input', function (evento) {
      campo.setCustomValidity('');
      // Ao apagar, não remonta a máscara (o hífen e os parênteses voltariam a cada tecla). Com o cursor no meio,
      // também não: o valor novo jogaria o cursor para o fim e a tecla seguinte entraria fora do lugar. O blur e
      // o envio remontam.
      if (evento.inputType && evento.inputType.indexOf('delete') === 0) return;
      if (campo.selectionStart !== campo.value.length) return;
      remascarar();
    });
    campo.addEventListener('blur', remascarar);
  }

  // ------------------------------------------------------------------
  // Conferência dos campos antes do envio (as mesmas regras do serviço, para não gastar tentativa).
  // ------------------------------------------------------------------
  function limparAoDigitar(campo) {
    campo.addEventListener('input', function () { campo.setCustomValidity(''); });
  }

  // Nome e empresa: uma linha, até 120 caracteres; só espaços não passa. Devolve o texto ou ''.
  function conferirTexto(campo) {
    var texto = umaLinha(campo.value, 1000);
    var mensagem = '';
    if (campo.value && !texto) mensagem = 'Preencha este campo.';
    else if (texto.length > NOME_MAXIMO) mensagem = 'Use até 120 caracteres.';
    campo.setCustomValidity(mensagem);
    return mensagem ? '' : texto;
  }

  // A forma de e-mail que o serviço aceita (microservico_raiox/validacao.py), em minúsculas.
  var FORMA_DO_EMAIL = /^[a-z0-9.!#$%&'*+\/=?^_`{|}~-]{1,64}@(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/;

  function conferirEmail(campo) {
    var texto = umaLinha(campo.value, 400).toLowerCase();
    var valido = texto.length <= EMAIL_MAXIMO && texto.indexOf('..') < 0 && FORMA_DO_EMAIL.test(texto);
    campo.setCustomValidity(texto && !valido ? 'Informe um e-mail válido.' : '');
    return valido ? texto : '';
  }

  // Devolve DDD e número ('' se inválido, com a mensagem no campo) e deixa o campo na máscara.
  function conferirTelefone(campo, mensagem) {
    var nacional = telefoneNacional(campo.value);
    if (nacional) campo.value = mascara(nacional);
    campo.setCustomValidity(nacional ? '' : mensagem);
    return nacional || '';
  }

  // ------------------------------------------------------------------
  // WhatsApp da equipe (só com o número desenhado pelo servidor) e avisos dos diálogos.
  // ------------------------------------------------------------------
  function numeroValido(numero) {
    return NUMERO_VALIDO.test(String(numero || ''));
  }

  function linkWhatsapp(numero, texto) {
    return numeroValido(numero) ? 'https://wa.me/' + numero + '?text=' + encodeURIComponent(texto) : '';
  }

  function linkNovaAba(elemento, rotulo, dica) {
    elemento.target = '_blank';
    elemento.rel = 'noopener noreferrer';
    elemento.textContent = rotulo;
    var escondido = document.createElement('span');
    escondido.className = 'visually-hidden';
    escondido.textContent = dica;
    elemento.appendChild(escondido);
    return elemento;
  }

  // Aviso de uma etapa (o contêiner tem role="alert" e fica vazio quando não há nada a dizer). opcoes:
  // {mensagem, tipo ('erro' ou 'info'), whatsapp: {numero, texto}, email, tentar: função}.
  function mostrarAviso(caixa, opcoes) {
    if (!caixa) return;
    caixa.replaceChildren();
    caixa.classList.toggle('is-info', opcoes.tipo === 'info');
    var texto = document.createElement('p');
    texto.textContent = opcoes.mensagem;
    caixa.appendChild(texto);
    var acoes = document.createElement('div');
    acoes.className = 'lp-aviso-acoes';
    if (typeof opcoes.tentar === 'function') {
      var tentar = document.createElement('button');
      tentar.type = 'button';
      tentar.className = 'btn-nexo btn-nexo-secondary';
      tentar.textContent = 'Tentar de novo';
      tentar.addEventListener('click', opcoes.tentar);
      acoes.appendChild(tentar);
    }
    var destino = opcoes.whatsapp ? linkWhatsapp(opcoes.whatsapp.numero, opcoes.whatsapp.texto) : '';
    if (destino) {
      var link = document.createElement('a');
      link.className = 'btn-nexo btn-nexo-secondary';
      link.href = destino;
      acoes.appendChild(linkNovaAba(link, 'Falar com a equipe pelo WhatsApp', ' (abre o WhatsApp em nova aba)'));
    } else if (opcoes.email) {
      var alternativa = document.createElement('p');
      alternativa.className = 'lp-aviso-email';
      alternativa.appendChild(document.createTextNode('Se preferir, escreva para '));
      var email = document.createElement('a');
      email.className = 'text-link';
      email.href = 'mailto:' + opcoes.email;
      email.textContent = opcoes.email;
      alternativa.appendChild(email);
      alternativa.appendChild(document.createTextNode('.'));
      caixa.appendChild(alternativa);
    }
    if (acoes.children.length) caixa.appendChild(acoes);
  }

  function esconderAviso(caixa) {
    if (!caixa) return;
    caixa.replaceChildren();
    caixa.classList.remove('is-info');
  }

  // "em cerca de N minutos" a partir do Retry-After em segundos.
  function textoDeEspera(segundos) {
    if (!segundos || segundos < 1) return '';
    if (segundos < 60) return 'em cerca de ' + segundos + (segundos === 1 ? ' segundo' : ' segundos');
    if (segundos < 3600) {
      var minutos = Math.ceil(segundos / 60);
      return 'em cerca de ' + minutos + (minutos === 1 ? ' minuto' : ' minutos');
    }
    var horas = Math.ceil(segundos / 3600);
    return 'em cerca de ' + horas + (horas === 1 ? ' hora' : ' horas');
  }

  // ------------------------------------------------------------------
  // Diálogo: abrir, fechar (Esc, botão ou clique que começou no fundo), trava da rolagem da página e foco de
  // volta em quem abriu. Durante um envio, o diálogo não fecha.
  // ------------------------------------------------------------------
  function controlarDialogo(dialogo, opcoes) {
    var origem = null;
    var travado = false;
    var fechadores = dialogo.querySelectorAll('[data-dialogo-fechar]');

    function abrir(gatilho) {
      if (dialogo.open) return;
      origem = gatilho || null;
      dialogo.showModal();
      document.documentElement.classList.add('lp-modal-aberto');
    }

    function fechar() {
      if (!travado && dialogo.open) dialogo.close();
    }

    dialogo.addEventListener('cancel', function (evento) {
      if (travado) evento.preventDefault();
    });
    dialogo.addEventListener('close', function () {
      document.documentElement.classList.remove('lp-modal-aberto');
      if (opcoes && typeof opcoes.aoFechar === 'function') opcoes.aoFechar();
      focar(origem);
    });
    // Só quando o clique COMEÇOU no fundo: selecionar um texto e soltar o mouse fora da caixa não fecha.
    var comecouNoFundo = false;
    dialogo.addEventListener('pointerdown', function (evento) {
      comecouNoFundo = evento.target === dialogo;
    });
    dialogo.addEventListener('click', function (evento) {
      if (evento.target === dialogo && comecouNoFundo) fechar();
      comecouNoFundo = false;
    });
    Array.prototype.forEach.call(fechadores, function (botao) {
      botao.addEventListener('click', fechar);
    });

    return {
      abrir: abrir,
      fechar: fechar,
      travar: function (sim) {
        travado = !!sim;
        Array.prototype.forEach.call(fechadores, function (botao) { botao.disabled = travado; });
      }
    };
  }

  // Um link comum (o "Cadastre-se", o CONTRATAR) passa a abrir o diálogo; o href fica como reserva.
  function transformarEmBotao(gatilho, aoAcionar) {
    gatilho.setAttribute('role', 'button');
    gatilho.setAttribute('aria-haspopup', 'dialog');
    gatilho.addEventListener('click', function (evento) {
      if (evento.button !== 0 || evento.metaKey || evento.ctrlKey || evento.shiftKey || evento.altKey) return;
      evento.preventDefault();
      aoAcionar(gatilho);
    });
    gatilho.addEventListener('keydown', function (evento) {
      if (evento.key === ' ' || evento.key === 'Spacebar') {
        evento.preventDefault();
        gatilho.click();
      }
    });
  }

  // ------------------------------------------------------------------
  // Documentos legais vigentes e a regra da rolagem.
  // ------------------------------------------------------------------
  function formatarData(iso) {
    var data = new Date(iso);
    if (!iso || isNaN(data.getTime())) return '';
    try {
      return data.toLocaleDateString('pt-BR', { timeZone: 'America/Sao_Paulo' });
    } catch (erro) {
      return data.toLocaleDateString('pt-BR');
    }
  }

  function lerDocumentos(corpo) {
    var achados = {};
    var lista = corpo && Array.isArray(corpo.documentos) ? corpo.documentos : [];
    lista.forEach(function (item) {
      var chave = item && Object.prototype.hasOwnProperty.call(DOCUMENTOS, item.tipo) ? DOCUMENTOS[item.tipo] : '';
      if (!chave || achados[chave]) return;
      var id = Number(item.id);
      if (!Number.isInteger(id) || id <= 0 || typeof item.conteudo !== 'string' || !item.conteudo.trim()) return;
      achados[chave] = {
        id: id,
        rotulo: umaLinha(item.rotulo, 20),
        publicadoEm: formatarData(item.publicado_em),
        conteudo: item.conteudo
      };
    });
    if (!achados.termos || !achados.privacidade) return { estado: 'sem_documentos' };
    return { estado: 'ok', termos: achados.termos, privacidade: achados.privacidade };
  }

  // {estado: 'ok', termos, privacidade} | {estado: 'sem_documentos'} (nada publicado ou rota inexistente) |
  // {estado: 'indisponivel'} (503 ou rede).
  function buscarDocumentos() {
    return fetch(ROTA_DOS_DOCUMENTOS, {
      credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' }
    }).then(function (resposta) {
      if (resposta.status === 404) return { estado: 'sem_documentos' };
      if (!resposta.ok) return { estado: 'indisponivel' };
      return resposta.json().then(lerDocumentos, function () { return { estado: 'indisponivel' }; });
    }, function () {
      return { estado: 'indisponivel' };
    });
  }

  // A etapa do aceite (marcação [data-termos] de landing.html). aoMudar avisa quando o "pronto" pode ter mudado.
  function Aceite(raiz, aoMudar) {
    var status = raiz.querySelector('[data-termos-status]');
    var itens = Array.prototype.map.call(raiz.querySelectorAll('[data-termo]'), function (bloco) {
      var dica = bloco.querySelector('[data-termo-dica]');
      return {
        chave: bloco.getAttribute('data-termo'),
        bloco: bloco,
        texto: bloco.querySelector('[data-termo-texto]'),
        versao: bloco.querySelector('[data-termo-versao]'),
        caixa: bloco.querySelector('[data-termo-aceite]'),
        dica: dica,
        dicaInicial: dica ? dica.textContent : '',
        lido: false
      };
    });
    var documentos = null;
    var geracao = 0;

    function avisar() {
      if (typeof aoMudar === 'function') aoMudar();
    }

    // Só conta com o texto na tela: escondido, as medidas são zero e a conta liberaria a caixa sem leitura.
    function conferirLeitura(item) {
      if (item.lido || !documentos || !item.texto.getClientRects().length) return;
      var texto = item.texto;
      if (texto.scrollTop + texto.clientHeight >= texto.scrollHeight - 2) {
        item.lido = true;
        item.caixa.disabled = false;
        if (item.dica) item.dica.textContent = 'Você chegou ao fim do texto: já pode marcar a caixa.';
        avisar();
      }
    }

    function conferir() {
      itens.forEach(conferirLeitura);
    }

    itens.forEach(function (item) {
      item.texto.addEventListener('scroll', function () { conferirLeitura(item); }, { passive: true });
      item.caixa.addEventListener('change', avisar);
    });
    window.addEventListener('resize', conferir);

    function limpar(mensagem) {
      documentos = null;
      itens.forEach(function (item) {
        item.bloco.hidden = true;
        item.texto.textContent = '';
        item.versao.textContent = '';
        item.caixa.checked = false;
        item.caixa.disabled = true;
        item.lido = false;
        if (item.dica) item.dica.textContent = item.dicaInicial;
      });
      status.textContent = mensagem || '';
      status.hidden = !mensagem;
      avisar();
    }

    function exibir(lidos) {
      documentos = lidos;
      status.textContent = '';
      status.hidden = true;
      itens.forEach(function (item) {
        var documento = lidos[item.chave];
        item.texto.textContent = documento.conteudo;
        item.texto.scrollTop = 0;
        item.versao.textContent = [documento.rotulo, documento.publicadoEm ? 'publicada em ' + documento.publicadoEm : '']
          .filter(Boolean).join(' · ');
        item.bloco.hidden = false;
      });
      window.requestAnimationFrame(conferir);
      avisar();
    }

    function carregar() {
      var minha = ++geracao;
      limpar('Carregando os termos…');
      return buscarDocumentos().then(function (resultado) {
        if (minha !== geracao) return { estado: 'cancelado' };
        if (resultado.estado === 'ok') exibir(resultado);
        else limpar('');
        return resultado;
      });
    }

    return {
      carregar: carregar,
      conferir: conferir,
      pronto: function () {
        return !!documentos && itens.every(function (item) { return item.caixa.checked; });
      },
      ids: function () {
        return documentos ? { termos: documentos.termos.id, privacidade: documentos.privacidade.id } : null;
      }
    };
  }

  // ------------------------------------------------------------------
  // Relatórios da Avaliação de Cenário Externo (marcação [data-arquivos] de landing.html).
  // ------------------------------------------------------------------
  function extensao(nome) {
    var texto = String(nome || '');
    var ponto = texto.lastIndexOf('.');
    return ponto >= 0 ? texto.slice(ponto + 1).toLowerCase() : '';
  }

  function problemaDoArquivo(arquivo) {
    var tipo = extensao(arquivo.name);
    if (EXTENSOES_COM_MACRO.indexOf(tipo) >= 0) {
      return 'Arquivo com macros (.xlsm) não é aceito: salve a planilha como .xlsx ou .csv.';
    }
    if (EXTENSOES_ACEITAS.indexOf(tipo) < 0) return 'Formato não aceito: envie .csv ou .xlsx.';
    if (!arquivo.size) return 'O arquivo está vazio.';
    if (arquivo.size > MAXIMO_DE_BYTES) return 'O arquivo passa de 15 MB.';
    return '';
  }

  function formatarTamanho(bytes) {
    if (bytes < 1024) return bytes + (bytes === 1 ? ' byte' : ' bytes');
    if (bytes < 1024 * 1024) return Math.max(1, Math.round(bytes / 1024)).toLocaleString('pt-BR') + ' KB';
    return (bytes / (1024 * 1024)).toLocaleString('pt-BR', { maximumFractionDigits: 1 }) + ' MB';
  }

  function Arquivos(raiz, aoMudar) {
    var campo = raiz.querySelector('[data-arquivos-campo]');
    var zona = raiz.querySelector('[data-arquivos-zona]');
    var lista = raiz.querySelector('[data-arquivos-lista]');
    var itens = [];
    var travado = false;

    function chave(arquivo) {
      return [arquivo.name, arquivo.size, arquivo.lastModified].join('|');
    }

    function desenhar() {
      lista.replaceChildren();
      itens.forEach(function (item, indice) {
        var linha = document.createElement('li');
        linha.className = 'lp-arquivo' + (item.problema ? ' is-recusado' : '');
        var nome = document.createElement('span');
        nome.className = 'lp-arquivo-nome';
        nome.textContent = item.arquivo.name;
        var tamanho = document.createElement('span');
        tamanho.className = 'lp-arquivo-tamanho';
        tamanho.textContent = formatarTamanho(item.arquivo.size);
        linha.appendChild(nome);
        linha.appendChild(tamanho);
        if (item.problema) {
          var motivo = document.createElement('span');
          motivo.className = 'lp-arquivo-motivo';
          motivo.textContent = item.problema;
          linha.appendChild(motivo);
        }
        var remover = document.createElement('button');
        remover.type = 'button';
        remover.className = 'lp-arquivo-remover';
        remover.textContent = 'Remover';
        remover.setAttribute('aria-label', 'Remover ' + item.arquivo.name);
        remover.disabled = travado;
        remover.addEventListener('click', function () {
          itens.splice(indice, 1);
          desenhar();
          focar(campo);
        });
        linha.appendChild(remover);
        lista.appendChild(linha);
      });
      if (typeof aoMudar === 'function') aoMudar();
    }

    function adicionar(novos) {
      Array.prototype.forEach.call(novos || [], function (arquivo) {
        var repetido = itens.some(function (item) { return chave(item.arquivo) === chave(arquivo); });
        if (!repetido) itens.push({ arquivo: arquivo, problema: problemaDoArquivo(arquivo) });
      });
      desenhar();
    }

    campo.addEventListener('change', function () {
      adicionar(campo.files);
      // Escolher de novo o mesmo arquivo dispara o change outra vez.
      campo.value = '';
    });
    if (zona) {
      ['dragenter', 'dragover'].forEach(function (nome) {
        zona.addEventListener(nome, function () { zona.classList.add('is-arrastando'); });
      });
      ['dragleave', 'drop'].forEach(function (nome) {
        zona.addEventListener(nome, function () { zona.classList.remove('is-arrastando'); });
      });
    }

    return {
      // {arquivos: [File]} pronto para o envio, ou {erro: 'texto para o visitante'}.
      situacao: function () {
        if (!itens.length) return { erro: 'Escolha ao menos um relatório (.csv ou .xlsx) para a avaliação.' };
        if (itens.some(function (item) { return item.problema; })) {
          return { erro: 'Remova os arquivos marcados antes de enviar.' };
        }
        if (itens.length > MAXIMO_DE_ARQUIVOS) return { erro: 'Envie no máximo 12 relatórios por vez.' };
        var total = itens.reduce(function (soma, item) { return soma + item.arquivo.size; }, 0);
        if (total > MAXIMO_DE_BYTES) return { erro: 'Os relatórios passam de 15 MB somados.' };
        return { arquivos: itens.map(function (item) { return item.arquivo; }) };
      },
      limpar: function () {
        itens = [];
        campo.value = '';
        desenhar();
      },
      travar: function (sim) {
        travado = !!sim;
        campo.disabled = travado;
        desenhar();
      },
      focar: function () { focar(campo); }
    };
  }

  // ------------------------------------------------------------------
  // Envio ao microsserviço e leitura da resposta.
  // ------------------------------------------------------------------
  function segundosDeEspera(valor) {
    var texto = String(valor || '').trim();
    if (/^\d+$/.test(texto)) return Number(texto);
    var data = Date.parse(texto);
    return isNaN(data) ? 0 : Math.max(0, Math.ceil((data - Date.now()) / 1000));
  }

  function lerResultado(corpo) {
    var arquivos = Array.isArray(corpo.arquivos) ? corpo.arquivos : [];
    return {
      tipo: 'ok',
      protocolo: umaLinha(corpo.protocolo, 20),
      statusArquivo: umaLinha(corpo.status_arquivo, 20),
      arquivos: arquivos.filter(function (item) { return item && typeof item === 'object'; }).map(function (item) {
        return {
          nome: umaLinha(item.nome, 160),
          aprovado: item.situacao === 'aprovado',
          motivo: umaLinha(item.motivo, 300)
        };
      })
    };
  }

  // {tipo: 'ok' | 'invalido' | 'termos' | 'grande' | 'limite' | 'indisponivel', ...}.
  function lerResposta(resposta) {
    var espera = segundosDeEspera(resposta.headers.get('Retry-After'));
    return resposta.json().catch(function () { return null; }).then(function (corpo) {
      corpo = corpo && typeof corpo === 'object' ? corpo : {};
      var mensagem = typeof corpo.erro === 'string' ? umaLinha(corpo.erro, 300) : '';
      var campo = typeof corpo.campo === 'string' ? umaLinha(corpo.campo, 40) : '';
      var status = resposta.status;
      if (status === 201) return lerResultado(corpo);
      if (status === 400 || status === 415) {
        return { tipo: 'invalido', campo: campo, mensagem: mensagem || 'Confira os dados e tente de novo.' };
      }
      if (status === 409) return { tipo: 'termos', mensagem: mensagem || MENSAGEM_TERMOS_ATUALIZADOS };
      if (status === 413) {
        return { tipo: 'grande', mensagem: mensagem || 'Os relatórios passam do permitido: até 15 MB somados.' };
      }
      if (status === 429) {
        return { tipo: 'limite', espera: espera, mensagem: mensagem || 'Muitos envios. Tente mais tarde.' };
      }
      return { tipo: 'indisponivel', espera: espera, mensagem: (status === 503 && mensagem) || MENSAGEM_INDISPONIVEL };
    });
  }

  // campos: os de texto do serviço (nome, empresa, email, whatsapp, interesse, termos_aceitos, documento_termos,
  // documento_privacidade, campo_isca); arquivos: [File] (só no interesse 1). Nunca rejeita: a falha vira
  // {tipo: 'rede'} (sem resposta, inclusive bloqueio de CORS) ou {tipo: 'tempo'} (prazo de 90 s).
  function enviarLead(servico, campos, arquivos) {
    if (!servico) return Promise.resolve({ tipo: 'indisponivel', mensagem: MENSAGEM_INDISPONIVEL });
    var dados = new FormData();
    Object.keys(campos).forEach(function (nome) { dados.append(nome, campos[nome]); });
    (arquivos || []).forEach(function (arquivo) { dados.append('arquivos', arquivo, arquivo.name); });
    var controle = typeof AbortController === 'function' ? new AbortController() : null;
    var prazo = controle ? window.setTimeout(function () { controle.abort(); }, PRAZO_DO_ENVIO) : 0;
    return fetch(servico + ROTA_DO_LEAD, {
      method: 'POST', body: dados, mode: 'cors', credentials: 'omit', cache: 'no-store', redirect: 'error',
      signal: controle ? controle.signal : undefined
    }).then(lerResposta).catch(function (erro) {
      return { tipo: erro && erro.name === 'AbortError' ? 'tempo' : 'rede' };
    }).then(function (resultado) {
      window.clearTimeout(prazo);
      return resultado;
    });
  }

  // O Render dorme o serviço sem uso: um GET ao /ping quando o diálogo abre adianta o despertar, para o envio
  // (minutos depois) não esperar a subida. Resposta opaca (no-cors), uma vez por página; falha não importa.
  var acordados = {};
  function acordarServico(servico) {
    if (!servico || acordados[servico]) return;
    acordados[servico] = true;
    try {
      fetch(servico + '/ping', { mode: 'no-cors', credentials: 'omit', cache: 'no-store' }).catch(function () {});
    } catch (erro) { /* sem rede: o envio trata a falha */ }
  }

  // Mensagem do aviso para cada falha do envio (o 400 de campo e o 409 cada diálogo trata à parte).
  function mensagemDaFalha(resultado) {
    if (resultado.tipo === 'rede') return MENSAGEM_REDE;
    if (resultado.tipo === 'tempo') return MENSAGEM_TEMPO;
    if (resultado.tipo === 'limite') {
      var quando = textoDeEspera(resultado.espera);
      return quando ? resultado.mensagem.replace(/\s*Tente mais tarde\.?$/, '') + ' Tente de novo ' + quando + '.'
        : resultado.mensagem;
    }
    return resultado.mensagem || MENSAGEM_INDISPONIVEL;
  }

  // Estado "enviando" de uma etapa: botões [data-acao] desabilitados, aviso [data-ocupado] visível e aria-busy.
  function ocupar(etapa, sim, texto) {
    etapa.setAttribute('aria-busy', sim ? 'true' : 'false');
    Array.prototype.forEach.call(etapa.querySelectorAll('[data-acao]'), function (botao) { botao.disabled = !!sim; });
    var aviso = etapa.querySelector('[data-ocupado]');
    if (!aviso) return;
    var rotulo = aviso.querySelector('[data-ocupado-texto]');
    if (rotulo && texto) rotulo.textContent = texto;
    aviso.hidden = !sim;
  }

  function textoOcupado(comRelatorios) {
    return comRelatorios ? 'Enviando… A conferência dos relatórios pode levar até 1 minuto.'
      : 'Enviando os seus dados…';
  }

  function suportaEnvio() {
    return typeof fetch === 'function' && typeof FormData === 'function' && typeof Promise === 'function';
  }

  // Lista do resultado: cada relatório com a situação e, se recusado, o motivo.
  function desenharArquivosDoResultado(lista, arquivos) {
    lista.replaceChildren();
    arquivos.forEach(function (item) {
      var linha = document.createElement('li');
      linha.className = 'lp-resultado-arquivo' + (item.aprovado ? ' is-aprovado' : ' is-recusado');
      var nome = document.createElement('span');
      nome.className = 'lp-arquivo-nome';
      nome.textContent = item.nome || 'Relatório sem nome';
      var situacao = document.createElement('span');
      situacao.className = 'lp-resultado-situacao';
      situacao.textContent = item.aprovado ? 'Aceito' : 'Recusado';
      linha.appendChild(nome);
      linha.appendChild(situacao);
      if (!item.aprovado && item.motivo) {
        var motivo = document.createElement('span');
        motivo.className = 'lp-arquivo-motivo';
        motivo.textContent = item.motivo;
        linha.appendChild(motivo);
      }
      lista.appendChild(linha);
    });
  }

  window.NexoCaptacao = {
    MENSAGEM_WHATSAPP: MENSAGEM_WHATSAPP,
    MENSAGEM_TERMOS_ATUALIZADOS: MENSAGEM_TERMOS_ATUALIZADOS,
    MENSAGEM_SEM_TERMOS: MENSAGEM_SEM_TERMOS,
    MENSAGEM_TERMOS_FORA: MENSAGEM_TERMOS_FORA,
    umaLinha: umaLinha,
    bloco: bloco,
    focar: focar,
    mascara: mascara,
    telefoneNacional: telefoneNacional,
    instalarMascara: instalarMascara,
    limparAoDigitar: limparAoDigitar,
    conferirTexto: conferirTexto,
    conferirEmail: conferirEmail,
    conferirTelefone: conferirTelefone,
    numeroValido: numeroValido,
    linkWhatsapp: linkWhatsapp,
    mostrarAviso: mostrarAviso,
    esconderAviso: esconderAviso,
    controlarDialogo: controlarDialogo,
    transformarEmBotao: transformarEmBotao,
    Aceite: Aceite,
    Arquivos: Arquivos,
    enviarLead: enviarLead,
    acordarServico: acordarServico,
    mensagemDaFalha: mensagemDaFalha,
    ocupar: ocupar,
    textoOcupado: textoOcupado,
    suportaEnvio: suportaEnvio,
    desenharArquivosDoResultado: desenharArquivosDoResultado
  };

  // ==================================================================
  // Lead frio: o "Cadastre-se" do topo abre o #cadastro-lead. Sem JavaScript (ou sem suporte a <dialog>), o
  // botão leva ao rodapé (#contato), com o e-mail e os canais da equipe.
  // ==================================================================
  (function () {
    var dialogo = document.getElementById('cadastro-lead');
    var gatilhos = document.querySelectorAll('[data-cadastro-gatilho]');
    if (!dialogo || !gatilhos.length || typeof dialogo.showModal !== 'function' || !suportaEnvio()) return;

    function porId(id) { return document.getElementById(id); }

    var servico = dialogo.getAttribute('data-servico') || '';
    var emailDeSuporte = dialogo.getAttribute('data-email') || '';
    var ajuda = dialogo.querySelector('[data-ajuda-whatsapp]');
    var numero = ajuda && numeroValido(ajuda.getAttribute('data-numero')) ? ajuda.getAttribute('data-numero') : '';
    var etapas = {
      termos: porId('cadastro-lead-etapa-1'),
      dados: porId('cadastro-lead-etapa-2'),
      avaliacao: porId('cadastro-lead-etapa-3'),
      resultado: porId('cadastro-lead-etapa-4')
    };
    var formulario = porId('cadastro-lead-form');
    var campos = {
      nome: porId('cadastro-lead-nome'),
      empresa: porId('cadastro-lead-empresa'),
      email: porId('cadastro-lead-email'),
      whatsapp: porId('cadastro-lead-telefone')
    };
    var isca = porId('cadastro-lead-isca');
    var continuar = porId('cadastro-lead-continuar');
    var voltar = etapas.avaliacao && etapas.avaliacao.querySelector('[data-voltar]');
    var faltando = !formulario || !continuar || !voltar || Object.keys(etapas).some(function (chave) {
      return !etapas[chave];
    }) || Object.keys(campos).some(function (chave) { return !campos[chave]; });
    if (faltando) return;

    var avisos = {
      termos: etapas.termos.querySelector('[data-aviso]'),
      dados: etapas.dados.querySelector('[data-aviso]'),
      avaliacao: etapas.avaliacao.querySelector('[data-aviso]')
    };
    var dados = null;
    var ocupado = false;
    var concluido = false;

    var aceite = Aceite(etapas.termos, function () { continuar.disabled = !aceite || !aceite.pronto(); });
    var relatorios = Arquivos(etapas.avaliacao.querySelector('[data-arquivos]'), function () {
      esconderAviso(avisos.avaliacao);
    });
    var controle = controlarDialogo(dialogo, {
      aoFechar: function () {
        // Fechado com o resultado na tela: o próximo cadastro começa do zero.
        if (concluido && !ocupado) reiniciar();
      }
    });

    instalarMascara(campos.whatsapp);
    [campos.nome, campos.empresa, campos.email].forEach(limparAoDigitar);

    function mostrar(nome, foco) {
      Object.keys(etapas).forEach(function (chave) {
        etapas[chave].classList.toggle('hidden', chave !== nome);
      });
      dialogo.setAttribute('aria-labelledby', etapas[nome].getAttribute('aria-labelledby'));
      focar(foco || etapas[nome].querySelector('.precadastro-titulo'));
      if (nome === 'termos') aceite.conferir();
    }

    // Mensagem do WhatsApp quando o site não consegue gravar: o contato não se perde.
    function mensagemWhatsapp() {
      if (!dados) return 'Olá! Quero me cadastrar no NEXO Faturamento Inteligente.';
      return ['*NOVO CONTATO | NEXO*', '', 'Nome: ' + dados.nome, 'Empresa: ' + dados.empresa,
        'E-mail: ' + dados.email, 'Telefone: ' + dados.telefone, '',
        'Origem: Cadastro da Landing Page da NEXO (o envio pelo site não foi concluído)'].join('\n');
    }

    function contatos() {
      return { whatsapp: numero ? { numero: numero, texto: mensagemWhatsapp() } : null, email: emailDeSuporte };
    }

    function carregarTermos(depois) {
      esconderAviso(avisos.termos);
      continuar.disabled = true;
      return aceite.carregar().then(function (resultado) {
        if (resultado.estado === 'ok') {
          if (typeof depois === 'function') depois();
          return;
        }
        if (resultado.estado === 'cancelado') return;
        var opcoes = contatos();
        opcoes.mensagem = resultado.estado === 'sem_documentos' ? MENSAGEM_SEM_TERMOS : MENSAGEM_TERMOS_FORA;
        if (resultado.estado === 'indisponivel') opcoes.tentar = function () { carregarTermos(); };
        mostrarAviso(avisos.termos, opcoes);
      });
    }

    function reiniciar() {
      dados = null;
      concluido = false;
      formulario.reset();
      Object.keys(campos).forEach(function (chave) { campos[chave].setCustomValidity(''); });
      relatorios.limpar();
      Object.keys(avisos).forEach(function (chave) { esconderAviso(avisos[chave]); });
    }

    function abrir(gatilho) {
      if (dialogo.open) return;
      controle.abrir(gatilho);
      acordarServico(servico);
      if (ocupado) {
        mostrar('avaliacao');
        return;
      }
      if (concluido) {
        mostrar('resultado');
        return;
      }
      // Fechado sem querer antes do envio: o que foi digitado fica; os termos vigentes são lidos de novo.
      Object.keys(avisos).forEach(function (chave) { esconderAviso(avisos[chave]); });
      mostrar('termos');
      carregarTermos();
    }

    continuar.addEventListener('click', function () {
      if (aceite.pronto()) mostrar('dados', campos.nome);
    });

    formulario.addEventListener('submit', function (evento) {
      evento.preventDefault();
      esconderAviso(avisos.dados);
      var nome = conferirTexto(campos.nome);
      var empresa = conferirTexto(campos.empresa);
      var email = conferirEmail(campos.email);
      var telefone = conferirTelefone(campos.whatsapp, MENSAGEM_TELEFONE);
      if (!formulario.reportValidity()) return;
      dados = { nome: nome, empresa: empresa, email: email, telefone: mascara(telefone) };
      mostrar('avaliacao');
    });

    voltar.addEventListener('click', function () {
      if (!ocupado) mostrar('dados', campos.nome);
    });

    function marcarCampo(nome, mensagem) {
      var campo = campos[nome];
      mostrar('dados', campo);
      var texto = nome === 'whatsapp' ? MENSAGEM_TELEFONE : mensagem;
      campo.setCustomValidity(texto);
      mostrarAviso(avisos.dados, { mensagem: 'Confira o campo marcado: ' + texto });
      campo.reportValidity();
    }

    function concluir(resultado, interesse) {
      concluido = true;
      porId('cadastro-lead-protocolo').textContent = resultado.protocolo || '—';
      var comRelatorios = interesse === '1';
      var lista = porId('cadastro-lead-resultado-arquivos');
      desenharArquivosDoResultado(lista, comRelatorios ? resultado.arquivos : []);
      Array.prototype.forEach.call(etapas.resultado.querySelectorAll('[data-com-relatorios]'), function (item) {
        item.hidden = !comRelatorios;
      });
      var texto = 'Recebemos o seu cadastro.';
      if (comRelatorios && resultado.statusArquivo === 'aprovado') {
        texto = 'Recebemos o seu cadastro e os relatórios.';
      } else if (comRelatorios && resultado.statusArquivo === 'parcial') {
        texto = 'Recebemos o seu cadastro e parte dos relatórios. Os recusados aparecem abaixo, com o motivo.';
      } else if (comRelatorios) {
        texto = 'Recebemos o seu cadastro, mas nenhum relatório passou na conferência. Veja o motivo de cada um ' +
          'abaixo.';
      }
      porId('cadastro-lead-resultado-texto').textContent = texto;
      mostrar('resultado');
    }

    function tratar(resultado, interesse) {
      if (resultado.tipo === 'ok') {
        concluir(resultado, interesse);
        return;
      }
      if (resultado.tipo === 'invalido' && Object.prototype.hasOwnProperty.call(campos, resultado.campo)) {
        marcarCampo(resultado.campo, resultado.mensagem);
        return;
      }
      if (resultado.tipo === 'termos' || (resultado.tipo === 'invalido' && resultado.campo === 'termos_aceitos')) {
        var aviso = resultado.tipo === 'termos' ? resultado.mensagem : MENSAGEM_TERMOS_ATUALIZADOS;
        mostrar('termos');
        carregarTermos(function () { mostrarAviso(avisos.termos, { mensagem: aviso, tipo: 'info' }); });
        return;
      }
      var opcoes = { mensagem: resultado.mensagem };
      if (resultado.tipo !== 'invalido' && resultado.tipo !== 'grande') {
        opcoes = contatos();
        opcoes.mensagem = mensagemDaFalha(resultado);
      }
      mostrarAviso(avisos.avaliacao, opcoes);
    }

    function enviar(interesse) {
      if (ocupado) return;
      esconderAviso(avisos.avaliacao);
      var ids = aceite.ids();
      if (!dados || !ids || !aceite.pronto()) {
        mostrar('termos');
        return;
      }
      var arquivos = [];
      if (interesse === '1') {
        var situacao = relatorios.situacao();
        if (situacao.erro) {
          mostrarAviso(avisos.avaliacao, { mensagem: situacao.erro });
          relatorios.focar();
          return;
        }
        arquivos = situacao.arquivos;
      }
      ocupado = true;
      controle.travar(true);
      relatorios.travar(true);
      ocupar(etapas.avaliacao, true, textoOcupado(arquivos.length > 0));
      enviarLead(servico, {
        nome: dados.nome, empresa: dados.empresa, email: dados.email, whatsapp: dados.telefone,
        interesse: interesse, termos_aceitos: 'true', documento_termos: String(ids.termos),
        documento_privacidade: String(ids.privacidade), campo_isca: isca ? isca.value : ''
      }, arquivos).then(function (resultado) {
        ocupado = false;
        controle.travar(false);
        relatorios.travar(false);
        ocupar(etapas.avaliacao, false);
        tratar(resultado, interesse);
      });
    }

    Array.prototype.forEach.call(etapas.avaliacao.querySelectorAll('[data-enviar]'), function (botao) {
      botao.addEventListener('click', function () { enviar(botao.getAttribute('data-enviar')); });
    });

    Array.prototype.forEach.call(gatilhos, function (gatilho) {
      var dica = gatilho.querySelector('.visually-hidden');
      if (dica) dica.textContent = ' (abre o cadastro)';
      transformarEmBotao(gatilho, abrir);
    });
  })();
})();
