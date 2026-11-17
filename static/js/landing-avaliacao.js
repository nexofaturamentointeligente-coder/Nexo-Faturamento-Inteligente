/*
 * NEXO — "Deixar Avaliação" da landing (COM-002; decisão do dono em 16/11/2026)
 * ============================================================
 * Só age com o <dialog id="avaliacao-modal">, que o servidor desenha para o cliente que pode avaliar
 * (templates/_avaliacao_landing.html). O "Deixar Avaliação" é um link para o formulário do portal; este
 * script o transforma no botão que abre o modal. Sem JavaScript, sem <dialog> ou sem fetch, o link continua
 * levando ao formulário do portal, que grava do mesmo jeito.
 *
 *   - Abre com showModal (o resto da página fica inerte), põe o foco nas estrelas e o devolve ao botão ao
 *     fechar. Esc, "Cancelar", o X e o clique no fundo fecham; Tab e Shift+Tab ficam presos no diálogo.
 *   - Abre sozinho quando a página chega com ?avaliar=1 (a volta do login por /avaliar); o parâmetro sai do
 *     endereço, para recarregar não abrir de novo.
 *   - Confere a nota (1 a 5) e o tamanho do comentário com as regras do servidor, que continua decidindo, e
 *     envia por fetch com X-Requested-With: fetch. Respostas: 200 recebida; 409 já avaliou; 422 erros por
 *     campo; 400 formulário expirado; 429 muitas tentativas; redirecionamento (sessão vencida ou termos a
 *     aceitar); 403, 503 e o resto, a mensagem do servidor ou a genérica.
 * Tudo que vem do servidor entra por textContent, nunca como HTML. Nada é guardado no navegador. Arquivo
 * separado do landing.js: uma falha aqui não derruba o menu, o carrossel nem os gráficos.
 */
(function () {
  'use strict';

  var dialogo = document.getElementById('avaliacao-modal');
  var gatilho = document.querySelector('[data-avaliacao-abrir]');
  var form = dialogo ? dialogo.querySelector('[data-avaliacao-form]') : null;
  if (!dialogo || !gatilho || !form || typeof dialogo.showModal !== 'function' || !window.fetch || !window.FormData) {
    return;
  }
  // Incluído duas vezes, o script não liga tudo em dobro.
  if (dialogo.hasAttribute('data-avaliacao-pronto')) return;
  dialogo.setAttribute('data-avaliacao-pronto', '');

  var TEMPO_LIMITE_MS = 30000;
  var CAMPOS = ['nota', 'comentario']; // na ordem da tela
  var MENSAGENS = {
    validacao: 'Confira os campos destacados.',
    nota: 'Selecione uma nota de 1 a 5 estrelas.',
    comentarioVazio: 'Conte como foi a sua experiência.',
    recebida: 'Recebemos sua avaliação. Ela será publicada depois da revisão da equipe.',
    recebidaSemPublicacao: 'Recebemos sua avaliação. Como você não autorizou a publicação, ela fica só com a equipe NEXO.',
    jaAvaliou: 'Você já enviou sua avaliação. Para consultá-la ou editá-la, abra Minha avaliação.',
    expirado: 'Este formulário expirou. Atualize a página e envie de novo.',
    tentativas: 'Muitas tentativas em pouco tempo. Aguarde alguns minutos e tente de novo. O que você escreveu continua aqui.',
    sessao: 'Não foi possível enviar: a sua sessão terminou ou há termos atualizados para aceitar. Se quiser, copie o seu comentário antes de continuar.',
    rede: 'Não foi possível conectar. Verifique a sua internet e tente de novo. O que você escreveu continua aqui.',
    servidor: 'Não foi possível enviar agora. Tente de novo em instantes. O que você escreveu continua aqui.',
    enviando: 'Enviando sua avaliação…'
  };

  function um(seletor) { return dialogo.querySelector(seletor); }

  var radios = form.querySelectorAll('input[name="nota"]');
  var grupoNota = document.getElementById('avaliacao-nota-grupo');
  var comentario = document.getElementById('avaliacao-comentario');
  var autoriza = document.getElementById('avaliacao-autoriza');
  var significado = um('[data-avaliacao-significado]');
  var contador = um('[data-avaliacao-contador]');
  var avisoLimite = um('[data-avaliacao-limite]');
  var resumo = um('[data-avaliacao-resumo]');
  var statusEnvio = um('[data-avaliacao-status]');
  var botaoEnviar = um('[data-avaliacao-enviar]');
  var rotuloEnviar = um('[data-avaliacao-enviar-rotulo]');
  var titulo = document.getElementById('avaliacao-modal-titulo');
  var intro = document.getElementById('avaliacao-modal-intro');
  var final = um('[data-avaliacao-final]');
  var finalTexto = um('[data-avaliacao-final-texto]');
  var finalLink = um('[data-avaliacao-final-link]');
  var botoesFechar = dialogo.querySelectorAll('[data-avaliacao-fechar]');
  if (!radios.length || !grupoNota || !comentario || !autoriza || !resumo || !botaoEnviar || !final) return;

  var rotuloOriginal = rotuloEnviar ? rotuloEnviar.textContent : '';
  var minhaAvaliacaoUrl = form.getAttribute('data-minha-avaliacao-url') || '';
  var avaliarUrl = form.getAttribute('data-avaliar-url') || '';
  var notaCta = document.getElementById('avaliacao-cta-nota');
  var enviando = false;
  var concluido = false;
  var errosAtuais = {};

  function focar(elemento) {
    if (!elemento) return;
    try { elemento.focus({ preventScroll: false }); } catch (erro) { elemento.focus(); }
  }

  // Só caminho local do próprio site (nada de //host ou /\host).
  function destinoLocal(destino) {
    return typeof destino === 'string' && /^\/(?![\/\\])/.test(destino) ? destino : '';
  }

  // ------------------------------------------------------------------
  // Nota e contador
  // ------------------------------------------------------------------
  function radioMarcado() { return form.querySelector('input[name="nota"]:checked'); }

  function atualizarSignificado() {
    var marcado = radioMarcado();
    var rotulo = marcado ? marcado.parentNode.querySelector('[data-avaliacao-rotulo]') : null;
    if (significado) significado.textContent = rotulo ? rotulo.textContent : '';
  }

  var faixaDoLimite = null;
  function atualizarContador() {
    if (!contador) return;
    // Mesma conta do maxlength do navegador: no limite, a digitação para.
    var maximo = comentario.maxLength, total = comentario.value.length, resta = maximo - total;
    contador.hidden = false;
    contador.textContent = total + '/' + maximo;
    contador.classList.toggle('is-perto', resta > 0 && resta <= 100);
    contador.classList.toggle('is-limite', resta <= 0);
    // O leitor de tela só ouve ao entrar numa faixa, nunca a cada tecla.
    var faixa = resta <= 0 ? 0 : resta <= 20 ? 20 : resta <= 100 ? 100 : null;
    if (faixa === faixaDoLimite || !avisoLimite) return;
    faixaDoLimite = faixa;
    avisoLimite.textContent = faixa === null ? '' : resta <= 0
      ? 'Limite de ' + maximo + ' caracteres atingido.' : 'Restam ' + resta + ' caracteres.';
  }

  // ------------------------------------------------------------------
  // Validação (as mesmas regras e mensagens do servidor, que continua decidindo)
  // ------------------------------------------------------------------
  function comprimento(valor) {
    var texto = (valor.normalize ? valor.normalize('NFC') : valor).trim();
    return Array.from ? Array.from(texto).length : texto.length; // o servidor conta caracteres
  }

  function mensagemDoCampo(campo) {
    if (campo === 'nota') return radioMarcado() ? '' : MENSAGENS.nota;
    var total = comprimento(comentario.value);
    if (!total) return MENSAGENS.comentarioVazio;
    if (total < comentario.minLength) return 'Escreva pelo menos ' + comentario.minLength + ' caracteres.';
    if (total > comentario.maxLength) return 'O comentário pode ter até ' + comentario.maxLength + ' caracteres.';
    return '';
  }

  function alvoAria(campo) { return campo === 'nota' ? grupoNota : comentario; }
  function alvoFoco(campo) { return campo === 'nota' ? (radioMarcado() || radios[0]) : comentario; }
  function caixaDeErro(campo) { return document.getElementById('avaliacao-' + campo + '-erro'); }

  function descrever(elemento, id, incluir) {
    var ids = (elemento.getAttribute('aria-describedby') || '').split(/\s+/).filter(function (item) {
      return item && item !== id;
    });
    if (incluir) ids.push(id);
    if (ids.length) elemento.setAttribute('aria-describedby', ids.join(' '));
    else elemento.removeAttribute('aria-describedby');
  }

  function marcarCampo(campo, mensagem) {
    var caixa = caixaDeErro(campo), alvo = alvoAria(campo);
    if (!caixa) return;
    caixa.textContent = mensagem;
    caixa.hidden = false;
    alvo.setAttribute('aria-invalid', 'true');
    descrever(alvo, caixa.id, true);
  }

  function limparCampo(campo) {
    var caixa = caixaDeErro(campo), alvo = alvoAria(campo);
    if (!caixa) return;
    caixa.textContent = '';
    caixa.hidden = true;
    alvo.removeAttribute('aria-invalid');
    descrever(alvo, caixa.id, false);
  }

  // ------------------------------------------------------------------
  // Resumo (role="alert"): erros por campo ou a mensagem do envio
  // ------------------------------------------------------------------
  function esvaziarResumo() {
    while (resumo.firstChild) resumo.removeChild(resumo.firstChild);
    resumo.hidden = true;
  }

  function mostrarResumoDeErros(erros) {
    var tituloResumo = document.createElement('p'), lista = document.createElement('ul');
    esvaziarResumo();
    tituloResumo.textContent = MENSAGENS.validacao;
    Object.keys(erros).forEach(function (campo) {
      var item = document.createElement('li');
      item.setAttribute('data-campo', campo);
      if (CAMPOS.indexOf(campo) !== -1) {
        var link = document.createElement('a');
        link.href = '#' + alvoFoco(campo).id;
        link.textContent = erros[campo];
        link.addEventListener('click', function (evento) {
          evento.preventDefault();
          focar(alvoFoco(campo));
        });
        item.appendChild(link);
      } else {
        item.textContent = String(erros[campo]);
      }
      lista.appendChild(item);
    });
    resumo.appendChild(tituloResumo);
    resumo.appendChild(lista);
    resumo.hidden = false;
  }

  function mostrarAlerta(mensagem, link) {
    var paragrafo = document.createElement('p');
    esvaziarResumo();
    paragrafo.textContent = mensagem;
    resumo.appendChild(paragrafo);
    if (link && link.href) {
      var ancora = document.createElement('a');
      ancora.href = link.href;
      ancora.textContent = link.texto;
      resumo.appendChild(ancora);
    }
    resumo.hidden = false;
  }

  // Campo corrigido sai do resumo sem reescrever o resto (que seria lido de novo).
  function revalidar(campo) {
    if (!errosAtuais[campo]) return;
    var mensagem = mensagemDoCampo(campo);
    var item = resumo.querySelector('[data-campo="' + campo + '"]');
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
    // Na ordem da tela: o primeiro do resumo é o primeiro campo inválido.
    var ordenados = {}, primeiro = null;
    CAMPOS.forEach(function (campo) {
      limparCampo(campo);
      if (!erros[campo]) return;
      ordenados[campo] = String(erros[campo]);
      marcarCampo(campo, ordenados[campo]);
      primeiro = primeiro || campo;
    });
    Object.keys(erros).forEach(function (campo) {
      if (CAMPOS.indexOf(campo) === -1) ordenados[campo] = String(erros[campo]);
    });
    errosAtuais = ordenados;
    mostrarResumoDeErros(ordenados);
    focar(primeiro ? alvoFoco(primeiro) : resumo);
  }

  // ------------------------------------------------------------------
  // Envio
  // ------------------------------------------------------------------
  function ocupar(sim) {
    enviando = sim;
    botaoEnviar.disabled = sim;
    Array.prototype.forEach.call(botoesFechar, function (botao) { botao.disabled = sim; });
    if (sim) {
      botaoEnviar.setAttribute('aria-busy', 'true');
      form.setAttribute('aria-busy', 'true');
    } else {
      botaoEnviar.removeAttribute('aria-busy');
      form.removeAttribute('aria-busy');
    }
    if (rotuloEnviar) rotuloEnviar.textContent = sim ? 'Enviando…' : rotuloOriginal;
    statusEnvio.textContent = sim ? MENSAGENS.enviando : '';
  }

  // O botão perdeu o foco ao ficar desabilitado: o foco vai para a mensagem, que é lida.
  function falhar(mensagem, link) {
    ocupar(false);
    mostrarAlerta(mensagem, link);
    focar(resumo);
  }

  // A avaliação existe no banco (recebida agora ou antes): o formulário dá lugar à mensagem e o
  // "Deixar Avaliação" da seção vira "Ver minha avaliação".
  function concluir(mensagem, destino) {
    concluido = true;
    ocupar(false);
    form.hidden = true;
    if (intro) intro.hidden = true;
    finalTexto.textContent = mensagem;
    if (finalTexto.id) dialogo.setAttribute('aria-describedby', finalTexto.id);
    if (finalLink) finalLink.href = destino || minhaAvaliacaoUrl;
    final.hidden = false;
    gatilho.href = minhaAvaliacaoUrl;
    gatilho.textContent = 'Ver minha avaliação';
    gatilho.removeAttribute('role');
    gatilho.removeAttribute('aria-haspopup');
    gatilho.removeAttribute('aria-controls');
    if (notaCta) notaCta.textContent = 'Você já enviou a sua avaliação. Obrigado!';
    focar(finalTexto);
  }

  function tratarJson(status, dados, autorizou) {
    var mensagem = typeof dados.mensagem === 'string' ? dados.mensagem : '';
    if (status === 200 && dados.ok === true) {
      concluir(mensagem || (autorizou ? MENSAGENS.recebida : MENSAGENS.recebidaSemPublicacao),
               destinoLocal(dados.destino));
    } else if (status === 409) {
      concluir(mensagem || MENSAGENS.jaAvaliou, destinoLocal(dados.destino));
    } else if (status === 422 && dados.erros && typeof dados.erros === 'object' && Object.keys(dados.erros).length) {
      ocupar(false);
      aplicarErros(dados.erros);
    } else {
      falhar(mensagem || MENSAGENS.servidor);
    }
  }

  function sessaoTerminou() {
    falhar(MENSAGENS.sessao, { href: destinoLocal(avaliarUrl), texto: 'Entrar e continuar' });
  }

  function tratarResposta(resposta, autorizou) {
    // Redirecionada: a sessão caiu (login) ou há termos a aceitar (a trava do portal); o fetch seguiu.
    if (resposta.redirected) {
      sessaoTerminou();
      return null;
    }
    var ehJson = (resposta.headers.get('Content-Type') || '').indexOf('application/json') !== -1;
    if (ehJson) {
      return resposta.json().then(function (dados) {
        tratarJson(resposta.status, dados || {}, autorizou);
      }, function () {
        falhar(MENSAGENS.servidor);
      });
    }
    if (resposta.status === 400) falhar(MENSAGENS.expirado);
    else if (resposta.status === 429) falhar(MENSAGENS.tentativas);
    else if (resposta.ok) sessaoTerminou(); // página com status 200: é a de login, não uma confirmação
    else falhar(MENSAGENS.servidor);
    return null;
  }

  function enviar() {
    // Sem o teto, um servidor mudo deixaria o botão ocupado para sempre. Reenviar depois é seguro: se o
    // primeiro envio tiver gravado, o servidor responde que a avaliação já existe.
    var controle = window.AbortController ? new AbortController() : null;
    var limite = controle ? window.setTimeout(function () { controle.abort(); }, TEMPO_LIMITE_MS) : null;
    var autorizou = autoriza.checked;
    ocupar(true);
    fetch(form.getAttribute('action'), {
      method: 'POST',
      body: new FormData(form),
      credentials: 'same-origin',
      headers: { 'X-Requested-With': 'fetch', 'Accept': 'application/json' },
      signal: controle ? controle.signal : undefined
    }).then(function (resposta) {
      return tratarResposta(resposta, autorizou);
    }).catch(function () {
      falhar(MENSAGENS.rede);
    }).then(function () {
      window.clearTimeout(limite);
    });
  }

  form.addEventListener('submit', function (evento) {
    evento.preventDefault();
    if (enviando || concluido) return;
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

  // ------------------------------------------------------------------
  // Abrir, fechar e prender o foco
  // ------------------------------------------------------------------
  function abrir() {
    if (dialogo.open || concluido) return;
    dialogo.showModal();
    document.documentElement.classList.add('avaliacao-aberta');
    focar(radioMarcado() || radios[0] || titulo);
  }

  function fechar() {
    if (dialogo.open && !enviando) dialogo.close();
  }

  dialogo.addEventListener('close', function () {
    document.documentElement.classList.remove('avaliacao-aberta');
    try { gatilho.focus({ preventScroll: true }); } catch (erro) { gatilho.focus(); }
  });

  // Esc fecha (o navegador dispara "cancel"), menos no meio do envio.
  dialogo.addEventListener('cancel', function (evento) {
    if (enviando) evento.preventDefault();
  });

  Array.prototype.forEach.call(botoesFechar, function (botao) {
    botao.addEventListener('click', fechar);
  });

  // Clique no fundo escurecido (fora da caixa) fecha. Só quando o clique COMEÇOU no fundo: selecionar um
  // texto e soltar o mouse fora da caixa não fecha.
  var cliqueComecouNoFundo = false;
  dialogo.addEventListener('pointerdown', function (evento) {
    cliqueComecouNoFundo = evento.target === dialogo;
  });
  dialogo.addEventListener('click', function (evento) {
    if (evento.target === dialogo && cliqueComecouNoFundo) fechar();
    cliqueComecouNoFundo = false;
  });

  // O que recebe Tab dentro do diálogo, visível e habilitado (os rádios ficam no meio, nunca nas pontas).
  function focaveis() {
    var candidatos = dialogo.querySelectorAll('a[href], button, textarea, input:not([type="radio"]), select');
    return Array.prototype.filter.call(candidatos, function (elemento) {
      return !elemento.disabled && elemento.getAttribute('tabindex') !== '-1' && elemento.getClientRects().length > 0;
    });
  }

  dialogo.addEventListener('keydown', function (evento) {
    if (evento.key !== 'Tab') return;
    var lista = focaveis();
    if (!lista.length) return;
    var primeiro = lista[0], ultimo = lista[lista.length - 1], ativo = document.activeElement;
    var dentro = dialogo.contains(ativo) && ativo !== dialogo;
    if (evento.shiftKey && (!dentro || ativo === primeiro)) {
      evento.preventDefault();
      focar(ultimo);
    } else if (!evento.shiftKey && (!dentro || ativo === ultimo)) {
      evento.preventDefault();
      focar(primeiro);
    }
  });

  // ------------------------------------------------------------------
  // O "Deixar Avaliação" passa a abrir o diálogo (o href do formulário do portal fica como reserva).
  // ------------------------------------------------------------------
  gatilho.setAttribute('role', 'button');
  gatilho.setAttribute('aria-haspopup', 'dialog');
  gatilho.setAttribute('aria-controls', 'avaliacao-modal');

  gatilho.addEventListener('click', function (evento) {
    if (concluido) return; // virou o link "Ver minha avaliação"
    if (evento.button !== 0 || evento.metaKey || evento.ctrlKey || evento.shiftKey || evento.altKey) return;
    evento.preventDefault();
    try {
      abrir();
    } catch (erro) {
      window.location.assign(gatilho.href);
    }
  });
  gatilho.addEventListener('keydown', function (evento) {
    if (concluido) return;
    if (evento.key === ' ' || evento.key === 'Spacebar') {
      evento.preventDefault();
      gatilho.click();
    }
  });

  Array.prototype.forEach.call(radios, function (radio) {
    radio.addEventListener('change', function () {
      atualizarSignificado();
      revalidar('nota');
    });
  });
  comentario.addEventListener('input', function () {
    atualizarContador();
    revalidar('comentario');
  });

  // Voltar pelo histórico (bfcache): a página reaparece como foi deixada, ainda ocupada.
  window.addEventListener('pageshow', function (evento) {
    if (evento.persisted && enviando) ocupar(false);
  });

  atualizarSignificado();
  atualizarContador();

  // A volta do login por /avaliar chega com ?avaliar=1: o modal abre sozinho e o parâmetro sai do endereço.
  (function abrirPeloEndereco() {
    var busca;
    try { busca = new URLSearchParams(window.location.search); } catch (erro) { return; }
    if (busca.get('avaliar') !== '1') return;
    busca.delete('avaliar');
    var resto = busca.toString();
    try {
      window.history.replaceState(window.history.state, '',
        window.location.pathname + (resto ? '?' + resto : '') + window.location.hash);
    } catch (erro) { /* sem History API: o parâmetro fica, e recarregar abre de novo */ }
    // Só depois do load: ao levar a página até #avaliacoes, o navegador tira o foco de onde estiver (a seção
    // não recebe foco). Aberto antes disso, o diálogo perderia o foco das estrelas.
    function abrirDepoisDoCarregamento() { window.setTimeout(abrir, 0); }
    if (document.readyState === 'complete') abrirDepoisDoCarregamento();
    else window.addEventListener('load', abrirDepoisDoCarregamento);
  })();
})();
