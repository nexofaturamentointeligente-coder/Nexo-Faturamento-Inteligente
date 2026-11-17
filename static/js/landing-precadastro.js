/*
 * NEXO — Pré-cadastro comercial da landing (decisões do dono, 15 e 16/11/2026)
 * ============================================================
 * O CONTRATAR do fim da página é um link wa.me real, desenhado pelo servidor (macro link_whatsapp, número de
 * config.WHATSAPP_EMPRESA_NUMERO). Este script o transforma no botão que abre o diálogo #precadastro:
 *   Etapa 1  aceite dos Termos de Uso e da Política de Privacidade vigentes, com a regra da rolagem;
 *   Etapa 2  dados: nome, empresa, WhatsApp, plano e e-mail (obrigatórios), faixa e dificuldade (opcionais);
 *   Etapa 3  "REALIZAR RAIO-X" abre aqui mesmo o envio dos relatórios (Avaliação de Cenário Externo, etapa 4,
 *            interesse 1); "PRECISO DE AJUDA COM OS RELATÓRIOS" grava o interesse 2; "FINALIZAR SEM RAIO-X", o 3.
 * As três saídas gravam o lead no microsserviço, pelas peças de static/js/landing-captacao.js
 * (window.NexoCaptacao). Gravado, a etapa 5 mostra o protocolo e o botão "Abrir o WhatsApp", com a mensagem de
 * sempre mais o protocolo. Se o envio falhar, a etapa mostra o motivo e ainda oferece o WhatsApp com a mensagem:
 * o contato não se perde. Plano, faixa de faturamento e dificuldade vão só na mensagem do WhatsApp (o serviço
 * não os grava).
 *
 * Sem o diálogo (número ausente ou inválido), sem suporte a <dialog>, sem o landing-captacao.js ou sem
 * JavaScript, nada muda: o CONTRATAR continua levando direto ao WhatsApp com a mensagem de contratação.
 * O número vem só do atributo data-numero, desenhado pelo servidor; ele nunca é fixo aqui.
 *
 * Exceção registrada à regra "a mensagem pré-preenchida nunca leva dado do cliente"
 * (Docs/WHATSAPP_EVOLUTION.md): aqui a mensagem leva só o que o próprio visitante digitou, e ele a confere no
 * WhatsApp antes de enviar.
 */
(function () {
  'use strict';

  var C = window.NexoCaptacao;
  var dialogo = document.getElementById('precadastro');
  var gatilho = document.querySelector('[data-precadastro-gatilho] a[href^="https://wa.me/"]');
  if (!C || !dialogo || !gatilho || typeof dialogo.showModal !== 'function' || !C.suportaEnvio()) return;
  var numero = dialogo.getAttribute('data-numero') || '';
  if (!C.numeroValido(numero)) return;
  var servico = dialogo.getAttribute('data-servico') || '';
  var emailDeSuporte = dialogo.getAttribute('data-email') || '';

  function porId(id) { return document.getElementById(id); }

  var etapas = { 1: porId('step-1'), 2: porId('step-2'), 3: porId('step-3'), 4: porId('step-4'), 5: porId('step-5') };
  var formulario = porId('form-pre-cadastro');
  var campoWhatsapp = porId('lead-whatsapp');
  var continuar = porId('btn-next-1');
  var abrirWhatsapp = porId('precadastro-whatsapp');
  var isca = porId('precadastro-isca');
  var botoes = {
    fazer: porId('btn-raiox-fazer'), ajuda: porId('btn-raiox-ajuda'), pular: porId('btn-raiox-pular'),
    enviar: porId('btn-raiox-enviar'), voltar: porId('btn-raiox-voltar')
  };
  var faltando = !formulario || !campoWhatsapp || !continuar || !abrirWhatsapp ||
    Object.keys(etapas).some(function (chave) { return !etapas[chave]; }) ||
    Object.keys(botoes).some(function (chave) { return !botoes[chave]; });
  if (faltando) return;

  // Situação do Raio-X na mensagem, por interesse (1 Raio-X, 2 ajuda com os relatórios, 3 só contato).
  var SITUACOES = {
    '1': { raioX: 'Relatórios enviados pelo site', auxilio: 'Não' },
    '2': { raioX: 'Não realizado', auxilio: 'Sim, cliente solicitou auxílio com relatórios' },
    '3': { raioX: 'Não solicitado', auxilio: 'Não' }
  };
  // A mensagem de contratação do próprio CONTRATAR (macro whatsapp_textos.contratar), para quando ainda não há
  // dados digitados (termos indisponíveis).
  var mensagemDeContratacao = (function () {
    try { return new URL(gatilho.href).searchParams.get('text') || ''; } catch (erro) { return ''; }
  })();

  var avisos = {};
  Object.keys(etapas).forEach(function (chave) { avisos[chave] = etapas[chave].querySelector('[data-aviso]'); });
  var dados = null;
  var ocupado = false;
  var etapaOcupada = 0;
  var concluido = false;

  var aceite = C.Aceite(etapas[1], function () { continuar.disabled = !aceite || !aceite.pronto(); });
  var relatorios = C.Arquivos(etapas[4].querySelector('[data-arquivos]'), function () { C.esconderAviso(avisos[4]); });
  var controle = C.controlarDialogo(dialogo, {
    aoFechar: function () {
      // Fechado com o resultado na tela: o próximo pré-cadastro começa do zero.
      if (concluido && !ocupado) reiniciar();
    }
  });

  C.instalarMascara(campoWhatsapp);
  [porId('lead-nome'), porId('lead-empresa'), porId('lead-email')].filter(Boolean).forEach(C.limparAoDigitar);

  // ------------------------------------------------------------------
  // Etapas
  // ------------------------------------------------------------------
  function mostrar(etapa, foco) {
    Object.keys(etapas).forEach(function (chave) {
      etapas[chave].classList.toggle('hidden', Number(chave) !== etapa);
    });
    dialogo.setAttribute('aria-labelledby', 'precadastro-titulo-' + etapa);
    C.focar(foco || etapas[etapa].querySelector('.precadastro-titulo'));
    if (etapa === 1) aceite.conferir();
  }

  // ------------------------------------------------------------------
  // Mensagem comercial (formato definido pelo dono) e o protocolo, quando o lead foi gravado.
  // ------------------------------------------------------------------
  function mensagem(interesse, protocolo) {
    if (!dados) return mensagemDeContratacao;
    var situacao = SITUACOES[interesse] || SITUACOES['3'];
    var raioX = interesse === '1' && !protocolo ? 'Envio dos relatórios pelo site não concluído' : situacao.raioX;
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
    linhas.push('', 'Situação do Raio-X: ' + raioX, 'Solicitou auxílio: ' + situacao.auxilio);
    if (protocolo) linhas.push('Protocolo: ' + protocolo);
    linhas.push('Origem: Landing Page da NEXO');
    return linhas.join('\n');
  }

  function oferecerWhatsapp(caixa, texto, interesse, extras) {
    var opcoes = {
      mensagem: texto,
      whatsapp: { numero: numero, texto: mensagem(interesse, '') },
      email: emailDeSuporte
    };
    Object.keys(extras || {}).forEach(function (chave) { opcoes[chave] = extras[chave]; });
    C.mostrarAviso(caixa, opcoes);
  }

  // ------------------------------------------------------------------
  // Etapa 1: os documentos vigentes e a regra da rolagem.
  // ------------------------------------------------------------------
  function carregarTermos(depois) {
    C.esconderAviso(avisos[1]);
    continuar.disabled = true;
    return aceite.carregar().then(function (resultado) {
      if (resultado.estado === 'ok') {
        if (typeof depois === 'function') depois();
        return;
      }
      if (resultado.estado === 'cancelado') return;
      var semDocumentos = resultado.estado === 'sem_documentos';
      oferecerWhatsapp(avisos[1], semDocumentos ? C.MENSAGEM_SEM_TERMOS : C.MENSAGEM_TERMOS_FORA, '3',
        semDocumentos ? null : { tentar: function () { carregarTermos(); } });
    });
  }

  continuar.addEventListener('click', function () {
    if (aceite.pronto()) mostrar(2, porId('lead-nome'));
  });

  // ------------------------------------------------------------------
  // Etapa 2: validação nativa do navegador mais as regras do serviço; nada sai daqui ainda.
  // ------------------------------------------------------------------
  formulario.addEventListener('submit', function (evento) {
    evento.preventDefault();
    C.esconderAviso(avisos[2]);
    var campos = formulario.elements;
    var nome = C.conferirTexto(campos.nome);
    var empresa = C.conferirTexto(campos.empresa);
    var email = C.conferirEmail(campos.email);
    var nacional = C.conferirTelefone(campoWhatsapp, C.MENSAGEM_WHATSAPP);
    if (!formulario.reportValidity()) return;
    dados = {
      nome: nome,
      empresa: empresa,
      whatsapp: C.mascara(nacional),
      plano: C.umaLinha(campos.plano.value, 40),
      email: email,
      faturamento: C.umaLinha(campos.faturamento.value, 40),
      desafio: C.bloco(campos.desafio.value, 400)
    };
    mostrar(3);
  });

  // ------------------------------------------------------------------
  // Etapas 3 e 4: as três saídas gravam o lead.
  // ------------------------------------------------------------------
  function marcarCampo(nome, texto) {
    var campo = formulario.elements[nome];
    mostrar(2, campo);
    var mensagemDoCampo = nome === 'whatsapp' ? C.MENSAGEM_WHATSAPP : texto;
    campo.setCustomValidity(mensagemDoCampo);
    C.mostrarAviso(avisos[2], { mensagem: 'Confira o campo marcado: ' + mensagemDoCampo });
    campo.reportValidity();
  }

  function concluir(resultado, interesse) {
    concluido = true;
    porId('precadastro-protocolo').textContent = resultado.protocolo || '—';
    var comRelatorios = interesse === '1';
    C.desenharArquivosDoResultado(porId('precadastro-resultado-arquivos'), comRelatorios ? resultado.arquivos : []);
    var texto = 'Seus dados foram gravados para a equipe NEXO.';
    if (comRelatorios && resultado.statusArquivo === 'aprovado') {
      texto = 'Seus dados e os relatórios foram gravados para a equipe NEXO.';
    } else if (comRelatorios && resultado.statusArquivo === 'parcial') {
      texto = 'Seus dados e parte dos relatórios foram gravados. Os recusados aparecem abaixo, com o motivo.';
    } else if (comRelatorios) {
      texto = 'Seus dados foram gravados, mas nenhum relatório passou na conferência. Veja o motivo de cada um ' +
        'abaixo.';
    }
    porId('precadastro-resultado-texto').textContent = texto;
    abrirWhatsapp.href = C.linkWhatsapp(numero, mensagem(interesse, resultado.protocolo));
    mostrar(5);
  }

  function tratar(resultado, interesse, etapa) {
    if (resultado.tipo === 'ok') {
      concluir(resultado, interesse);
      return;
    }
    var campos = ['nome', 'empresa', 'email', 'whatsapp'];
    if (resultado.tipo === 'invalido' && campos.indexOf(resultado.campo) >= 0) {
      marcarCampo(resultado.campo, resultado.mensagem);
      return;
    }
    if (resultado.tipo === 'termos' || (resultado.tipo === 'invalido' && resultado.campo === 'termos_aceitos')) {
      var aviso = resultado.tipo === 'termos' ? resultado.mensagem : C.MENSAGEM_TERMOS_ATUALIZADOS;
      mostrar(1);
      carregarTermos(function () { C.mostrarAviso(avisos[1], { mensagem: aviso, tipo: 'info' }); });
      return;
    }
    oferecerWhatsapp(avisos[etapa], C.mensagemDaFalha(resultado), interesse);
  }

  function enviar(interesse) {
    if (ocupado) return;
    var etapa = interesse === '1' ? 4 : 3;
    C.esconderAviso(avisos[etapa]);
    var ids = aceite.ids();
    if (!dados || !ids || !aceite.pronto()) {
      mostrar(1);
      return;
    }
    var arquivos = [];
    if (interesse === '1') {
      var situacao = relatorios.situacao();
      if (situacao.erro) {
        C.mostrarAviso(avisos[4], { mensagem: situacao.erro });
        relatorios.focar();
        return;
      }
      arquivos = situacao.arquivos;
    }
    ocupado = true;
    etapaOcupada = etapa;
    controle.travar(true);
    relatorios.travar(true);
    C.ocupar(etapas[etapa], true, C.textoOcupado(arquivos.length > 0));
    C.enviarLead(servico, {
      nome: dados.nome, empresa: dados.empresa, email: dados.email, whatsapp: dados.whatsapp,
      interesse: interesse, termos_aceitos: 'true', documento_termos: String(ids.termos),
      documento_privacidade: String(ids.privacidade), campo_isca: isca ? isca.value : ''
    }, arquivos).then(function (resultado) {
      ocupado = false;
      controle.travar(false);
      relatorios.travar(false);
      C.ocupar(etapas[etapa], false);
      tratar(resultado, interesse, etapa);
    });
  }

  botoes.fazer.addEventListener('click', function () {
    if (!ocupado) mostrar(4);
  });
  botoes.voltar.addEventListener('click', function () {
    if (!ocupado) mostrar(3);
  });
  botoes.ajuda.addEventListener('click', function () { enviar('2'); });
  botoes.pular.addEventListener('click', function () { enviar('3'); });
  botoes.enviar.addEventListener('click', function () { enviar('1'); });

  // ------------------------------------------------------------------
  // Abrir e recomeçar
  // ------------------------------------------------------------------
  function reiniciar() {
    dados = null;
    concluido = false;
    formulario.reset();
    Array.prototype.forEach.call(formulario.elements, function (campo) {
      if (typeof campo.setCustomValidity === 'function') campo.setCustomValidity('');
    });
    relatorios.limpar();
    Object.keys(avisos).forEach(function (chave) { C.esconderAviso(avisos[chave]); });
    abrirWhatsapp.removeAttribute('href');
  }

  function abrir() {
    if (dialogo.open) return;
    controle.abrir(gatilho);
    C.acordarServico(servico);
    if (ocupado) {
      mostrar(etapaOcupada);
      return;
    }
    if (concluido) {
      mostrar(5);
      return;
    }
    // Fechado sem querer antes do envio: o que foi digitado fica; os termos vigentes são lidos de novo.
    Object.keys(avisos).forEach(function (chave) { C.esconderAviso(avisos[chave]); });
    mostrar(1);
    carregarTermos();
  }

  // ------------------------------------------------------------------
  // O CONTRATAR passa a abrir o diálogo (o href wa.me fica como reserva).
  // ------------------------------------------------------------------
  var dicaGatilho = gatilho.querySelector('.visually-hidden');
  if (dicaGatilho) dicaGatilho.textContent = ' (abre o pré-cadastro)';
  C.transformarEmBotao(gatilho, function () {
    try {
      abrir();
    } catch (erro) {
      window.open(gatilho.href, '_blank', 'noopener');
    }
  });
})();
