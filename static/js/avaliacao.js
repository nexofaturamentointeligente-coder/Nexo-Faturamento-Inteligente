/* NEXO — formulário de avaliação do cliente (COM-002)
 * ------------------------------------------------------------------
 * O formulário funciona sozinho (POST normal, erros reexibidos pelo servidor;
 * estrelas e bloco de identificação são só CSS). Este arquivo acrescenta:
 * - o significado da nota selecionada, em região aria-live;
 * - contadores de caracteres, com aviso ao leitor de tela só perto do limite;
 * - validação antes do envio: erro por campo, resumo e foco no primeiro inválido;
 * - envio por fetch com todos os estados. Sucesso só depois da resposta 200: o
 *   flash já está na sessão e a página de destino o exibe;
 * - rascunho na sessionStorage quando a sessão cai: o cliente precisa sair da
 *   página para entrar de novo e o texto volta depois. Vale só para esta aba e
 *   este usuário; as autorizações nunca são restauradas, precisam ser marcadas de novo.
 * Tudo que vem do servidor entra como texto (textContent), nunca como HTML.
 */
(function () {
  'use strict';

  var form = document.getElementById('formAvaliacao');
  /* Sem fetch segue o POST normal: o servidor valida e responde com a página. */
  if (!form || !window.fetch || !window.FormData) return;

  var TEMPO_LIMITE_MS = 30000;
  var CHAVE_RASCUNHO = 'nexo.avaliacao.rascunho';
  var CAMPOS = ['nota', 'comentario', 'sugestao', 'nome_exibicao']; /* ordem da tela */
  var MENSAGENS = {
    validacao: 'Confira os campos destacados.',
    formularioInvalido: 'Sua sessão expirou ou o formulário é inválido. Atualize a página e tente novamente.',
    muitasTentativas: 'Muitas tentativas em pouco tempo. Aguarde alguns minutos e tente novamente. Seus dados continuam no formulário.',
    sessao: 'Sua sessão expirou. Entre novamente para enviar.',
    rede: 'Não foi possível conectar ao servidor. Verifique sua internet e tente novamente. Seus dados continuam no formulário.',
    servidor: 'Não foi possível processar o envio agora. Seus dados continuam no formulário; tente novamente em instantes.',
    rascunhoGuardado: ' O que você digitou será recuperado ao voltar para esta página.'
  };

  var radios = form.querySelectorAll('input[name="nota"]'),
      grupoNota = document.getElementById('notaGrupo'),
      significado = document.getElementById('notaSignificado'),
      comentario = document.getElementById('comentario'),
      sugestao = document.getElementById('sugestao'),
      nomeExibicao = document.getElementById('nome_exibicao'),
      consentimento = document.getElementById('autoriza_publicacao'),
      resumo = document.getElementById('avaliacaoResumo'),
      resumoConteudo = document.getElementById('avaliacaoResumoConteudo'),
      statusEnvio = document.getElementById('avaliacaoStatus'),
      botao = document.getElementById('avaliacaoEnviar'),
      botaoRotulo = document.getElementById('avaliacaoEnviarRotulo'),
      rotuloOriginal = botaoRotulo.textContent;

  var enviando = false;
  var errosAtuais = {};

  /* ---------- nota ---------- */
  function radioMarcado() { return form.querySelector('input[name="nota"]:checked'); }
  function atualizarSignificado() {
    var marcado = radioMarcado();
    significado.textContent = marcado ? marcado.parentNode.querySelector('.estrela-texto').textContent : '';
  }

  /* ---------- contadores ---------- */
  function prepararContador(campo) {
    var contador = document.getElementById(campo.id + 'Contador'),
        aviso = document.getElementById(campo.id + 'Limite'),
        maximo = campo.maxLength,
        faixa = null;
    contador.hidden = false;
    function atualizar() {
      /* Mesma conta do maxlength do navegador: 1000/1000 é exatamente onde a digitação para. */
      var total = campo.value.length, resta = maximo - total;
      contador.textContent = total + '/' + maximo;
      contador.classList.toggle('is-perto', resta > 0 && resta <= 100);
      contador.classList.toggle('is-limite', resta <= 0);
      /* O leitor de tela só ouve ao entrar numa faixa, nunca a cada tecla. */
      var nova = resta <= 0 ? 0 : resta <= 20 ? 20 : resta <= 100 ? 100 : null;
      if (nova === faixa) return;
      faixa = nova;
      aviso.textContent = nova === null ? '' : resta <= 0
        ? 'Limite de ' + maximo + ' caracteres atingido.'
        : 'Restam ' + resta + ' caracteres.';
    }
    campo.addEventListener('input', atualizar);
    atualizar();
  }

  /* ---------- validação (mesmas regras e mensagens do servidor, que continua decidindo) ---------- */
  function comprimento(valor) {
    var texto = (valor.normalize ? valor.normalize('NFC') : valor).trim();
    return Array.from(texto).length; /* o servidor conta caracteres, não unidades UTF-16 */
  }
  function mensagemDoCampo(campo) {
    var total;
    if (campo === 'nota') return radioMarcado() ? '' : 'Selecione uma nota de 1 a 5 estrelas.';
    if (campo === 'comentario') {
      total = comprimento(comentario.value);
      if (!total) return 'Conte como foi sua experiência.';
      if (total < comentario.minLength) return 'Escreva pelo menos ' + comentario.minLength + ' caracteres.';
      if (total > comentario.maxLength) return 'O comentário pode ter até ' + comentario.maxLength + ' caracteres.';
    }
    if (campo === 'sugestao' && comprimento(sugestao.value) > sugestao.maxLength) {
      return 'A sugestão pode ter até ' + sugestao.maxLength + ' caracteres.';
    }
    /* nome_exibicao fica de fora: o bloco de identificação nunca bloqueia o envio. */
    return '';
  }

  /* ---------- erros por campo ---------- */
  function alvoAria(campo) { return campo === 'nota' ? grupoNota : document.getElementById(campo); }
  function alvoFoco(campo) { return campo === 'nota' ? (radioMarcado() || radios[0]) : document.getElementById(campo); }
  function descrever(elemento, id, incluir) {
    var ids = (elemento.getAttribute('aria-describedby') || '').split(/\s+/).filter(function (item) {
      return item && item !== id;
    });
    if (incluir) ids.push(id);
    if (ids.length) elemento.setAttribute('aria-describedby', ids.join(' '));
    else elemento.removeAttribute('aria-describedby');
  }
  function marcarCampo(campo, mensagem) {
    var caixa = document.getElementById(campo + 'Erro'), alvo = alvoAria(campo);
    if (!caixa || !alvo) return;
    caixa.querySelector('span').textContent = mensagem;
    caixa.hidden = false;
    alvo.setAttribute('aria-invalid', 'true');
    descrever(alvo, caixa.id, true);
  }
  function limparCampo(campo) {
    var caixa = document.getElementById(campo + 'Erro'), alvo = alvoAria(campo);
    if (!caixa || !alvo) return;
    caixa.querySelector('span').textContent = '';
    caixa.hidden = true;
    alvo.removeAttribute('aria-invalid');
    descrever(alvo, caixa.id, false);
  }

  /* ---------- resumo (role="alert") ---------- */
  function esvaziarResumo() {
    while (resumoConteudo.firstChild) resumoConteudo.removeChild(resumoConteudo.firstChild);
    resumo.hidden = true;
  }
  function mostrarResumoDeErros(erros) {
    var titulo = document.createElement('strong'), lista = document.createElement('ul');
    esvaziarResumo();
    titulo.textContent = MENSAGENS.validacao;
    Object.keys(erros).forEach(function (campo) {
      var item = document.createElement('li');
      item.setAttribute('data-campo', campo);
      if (CAMPOS.indexOf(campo) !== -1) {
        var link = document.createElement('a');
        link.href = '#' + alvoFoco(campo).id;
        link.textContent = erros[campo];
        item.appendChild(link);
      } else {
        item.textContent = erros[campo];
      }
      lista.appendChild(item);
    });
    resumoConteudo.appendChild(titulo);
    resumoConteudo.appendChild(lista);
    resumo.hidden = false;
  }
  function mostrarAlerta(mensagem, link) {
    var paragrafo = document.createElement('p');
    esvaziarResumo();
    paragrafo.textContent = mensagem;
    resumoConteudo.appendChild(paragrafo);
    if (link) {
      var ancora = document.createElement('a');
      ancora.href = link.href;
      ancora.textContent = link.texto;
      resumoConteudo.appendChild(ancora);
    }
    resumo.hidden = false;
  }

  /* Campo corrigido sai do resumo sem reescrever o resto (que seria lido de novo). */
  function revalidar(campo) {
    if (!errosAtuais[campo]) return;
    var mensagem = mensagemDoCampo(campo);
    var item = resumoConteudo.querySelector('[data-campo="' + campo + '"]');
    if (mensagem) {
      if (mensagem === errosAtuais[campo]) return;
      errosAtuais[campo] = mensagem;
      marcarCampo(campo, mensagem);
      if (item && item.firstChild) item.firstChild.textContent = mensagem;
      return;
    }
    delete errosAtuais[campo];
    limparCampo(campo);
    if (item) item.parentNode.removeChild(item);
    if (!Object.keys(errosAtuais).length) esvaziarResumo();
  }

  function aplicarErros(erros) {
    /* Na ordem da tela: o primeiro do resumo é o primeiro campo inválido. */
    var ordenados = {}, primeiro = null;
    CAMPOS.forEach(function (campo) {
      limparCampo(campo);
      if (!erros[campo]) return;
      ordenados[campo] = erros[campo];
      marcarCampo(campo, erros[campo]);
      primeiro = primeiro || campo;
    });
    Object.keys(erros).forEach(function (campo) {
      if (CAMPOS.indexOf(campo) === -1) ordenados[campo] = erros[campo];
    });
    errosAtuais = ordenados;
    mostrarResumoDeErros(ordenados);
    (primeiro ? alvoFoco(primeiro) : resumo).focus();
  }

  /* ---------- rascunho (só quando a sessão cai) ---------- */
  function dono() { return form.getAttribute('data-usuario'); }
  function guardarRascunho() {
    var marcado = radioMarcado();
    try {
      sessionStorage.setItem(CHAVE_RASCUNHO, JSON.stringify({
        dono: dono(), acao: form.getAttribute('action'), nota: marcado ? marcado.value : '',
        comentario: comentario.value, sugestao: sugestao.value, nome_exibicao: nomeExibicao.value
      }));
      return true;
    } catch (_) { return false; }
  }
  function apagarRascunho() {
    try { sessionStorage.removeItem(CHAVE_RASCUNHO); } catch (_) { /* navegador sem armazenamento */ }
  }
  /* Vale uma vez, para o mesmo usuário e a mesma tela; qualquer outro caso é descartado. */
  function recuperarRascunho() {
    var rascunho = null;
    try { rascunho = JSON.parse(sessionStorage.getItem(CHAVE_RASCUNHO)); } catch (_) { rascunho = null; }
    apagarRascunho();
    if (!rascunho || rascunho.dono !== dono() || rascunho.acao !== form.getAttribute('action')) return;
    Array.prototype.forEach.call(radios, function (radio) {
      if (radio.value === rascunho.nota) radio.checked = true;
    });
    comentario.value = rascunho.comentario || '';
    sugestao.value = rascunho.sugestao || '';
    nomeExibicao.value = rascunho.nome_exibicao || '';
    document.getElementById('avaliacaoRascunhoTexto').textContent =
      'Recuperamos o que você havia digitado. Confira os campos antes de enviar' +
      (consentimento.checked ? '.' : ' e, se quiser publicar, marque de novo a autorização.');
    document.getElementById('avaliacaoRascunho').hidden = false;
  }

  /* ---------- envio ---------- */
  function ocupar(sim) {
    enviando = sim;
    botao.disabled = sim;
    if (sim) {
      botao.setAttribute('aria-busy', 'true');
      form.setAttribute('aria-busy', 'true');
    } else {
      botao.removeAttribute('aria-busy');
      form.removeAttribute('aria-busy');
    }
    botaoRotulo.textContent = sim ? 'Enviando…' : rotuloOriginal;
    statusEnvio.textContent = sim ? 'Enviando sua avaliação…' : '';
  }
  /* O botão perdeu o foco ao ficar desabilitado: volta para ele, ao lado da mensagem. */
  function falhar(mensagem, link) {
    ocupar(false);
    mostrarAlerta(mensagem, link);
    botao.focus();
  }
  function sessaoExpirada() {
    var guardado = guardarRascunho();
    falhar(MENSAGENS.sessao + (guardado ? MENSAGENS.rascunhoGuardado : ''),
           { href: form.getAttribute('data-avaliar-url'), texto: 'Entrar novamente' });
  }
  function irPara(destino) {
    var local = typeof destino === 'string' && /^\/(?![\/\\])/.test(destino);
    window.location.assign(local ? destino : form.getAttribute('data-destino'));
  }

  function tratarJson(status, dados) {
    if ((status === 200 && dados.ok === true) || (status === 409 && dados.destino)) {
      /* Fica ocupado até a navegação: nada de segundo envio nem de sucesso antecipado. */
      apagarRascunho();
      irPara(dados.destino);
    } else if (status === 422 && dados.erros && Object.keys(dados.erros).length) {
      ocupar(false);
      aplicarErros(dados.erros);
    } else {
      falhar(dados.mensagem || MENSAGENS.servidor);
    }
  }
  function tratarResposta(resposta) {
    /* Redirecionada: a sessão caiu e o fetch seguiu até a tela de login. */
    if (resposta.redirected) { sessaoExpirada(); return null; }
    var ehJson = (resposta.headers.get('Content-Type') || '').indexOf('application/json') !== -1;
    if (ehJson) {
      return resposta.json().then(function (dados) {
        tratarJson(resposta.status, dados || {});
      }, function () {
        falhar(MENSAGENS.servidor);
      });
    }
    if (resposta.status === 400) {
      falhar(MENSAGENS.formularioInvalido + (guardarRascunho() ? MENSAGENS.rascunhoGuardado : ''));
    } else if (resposta.status === 429) {
      falhar(MENSAGENS.muitasTentativas);
    } else if (resposta.ok) {
      sessaoExpirada(); /* HTML com status 200: é a tela de login, não uma confirmação */
    } else {
      falhar(MENSAGENS.servidor);
    }
    return null;
  }

  function enviar() {
    /* Sem o teto, um servidor mudo deixaria o botão ocupado para sempre. Reenviar depois
       é seguro: se o primeiro envio tiver gravado, o servidor responde "já registrada". */
    var controle = window.AbortController ? new AbortController() : null;
    var limite = controle ? window.setTimeout(function () { controle.abort(); }, TEMPO_LIMITE_MS) : null;
    ocupar(true);
    fetch(form.getAttribute('action'), {
      method: 'POST',
      body: new FormData(form),
      credentials: 'same-origin',
      headers: { 'X-Requested-With': 'fetch', 'Accept': 'application/json' },
      signal: controle ? controle.signal : undefined
    }).then(tratarResposta).catch(function () {
      falhar(MENSAGENS.rede);
    }).then(function () {
      window.clearTimeout(limite);
    });
  }

  form.addEventListener('submit', function (evento) {
    evento.preventDefault();
    if (enviando) return;
    var erros = {};
    CAMPOS.forEach(function (campo) {
      var mensagem = mensagemDoCampo(campo);
      if (mensagem) erros[campo] = mensagem;
    });
    if (Object.keys(erros).length) {
      aplicarErros(erros);
      return;
    }
    CAMPOS.forEach(limparCampo);
    errosAtuais = {};
    esvaziarResumo();
    enviar();
  });

  /* ---------- início ---------- */
  CAMPOS.forEach(function (campo) {
    var caixa = document.getElementById(campo + 'Erro');
    if (caixa && !caixa.hidden) errosAtuais[campo] = caixa.querySelector('span').textContent;
  });
  recuperarRascunho();
  atualizarSignificado();
  prepararContador(comentario);
  prepararContador(sugestao);

  Array.prototype.forEach.call(radios, function (radio) {
    radio.addEventListener('change', function () {
      atualizarSignificado();
      revalidar('nota');
    });
  });
  comentario.addEventListener('input', function () { revalidar('comentario'); });
  sugestao.addEventListener('input', function () { revalidar('sugestao'); });
  nomeExibicao.addEventListener('input', function () { revalidar('nome_exibicao'); });

  /* Voltar pelo histórico (bfcache): a página reaparece como foi deixada, ainda ocupada. */
  window.addEventListener('pageshow', function (evento) {
    if (evento.persisted) ocupar(false);
  });
})();
