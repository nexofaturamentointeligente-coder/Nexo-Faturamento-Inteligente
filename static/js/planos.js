/* Planos por nível (entitlements.py): convite de upgrade sobre os dados protegidos e o envio
   de vários relatórios do Raio-X. Só lê atributos do HTML; nenhum valor vem do servidor. */
(function () {
  'use strict';

  /* ---- Convite sobre o dado protegido ----
     Abre ao passar o mouse ou receber o foco, continua aberto enquanto o ponteiro estiver
     sobre ele e fecha com Esc (WCAG 1.4.13). Sem o Bootstrap, fica o title do link. */
  var fechamentos = [];
  if (window.bootstrap && window.bootstrap.Popover) {
    document.querySelectorAll('[data-nexo-convite]').forEach(function (gatilho) {
      var popover = window.bootstrap.Popover.getOrCreateInstance(gatilho, {
        trigger: 'manual', placement: 'top', container: 'body'
      });
      var espera = null;
      // Intenção de manter aberto: se o ponteiro ou o foco voltam durante o fade de saída,
      // o convite reabre quando o Bootstrap termina de esconder.
      var querAberto = false;
      function balao() {
        var id = gatilho.getAttribute('aria-describedby');
        return id ? document.getElementById(id) : null;
      }
      function mostrar() {
        querAberto = true;
        window.clearTimeout(espera);
        if (!balao()) popover.show();
      }
      function fechar() {
        querAberto = false;
        window.clearTimeout(espera);
        popover.hide();
      }
      function esconderDepois() {
        window.clearTimeout(espera);
        espera = window.setTimeout(function () {
          var tip = balao();
          if (gatilho.matches(':hover') || document.activeElement === gatilho || (tip && tip.matches(':hover'))) return;
          fechar();
        }, 150);
      }
      gatilho.addEventListener('mouseenter', mostrar);
      gatilho.addEventListener('focus', mostrar);
      gatilho.addEventListener('mouseleave', esconderDepois);
      gatilho.addEventListener('blur', esconderDepois);
      gatilho.addEventListener('shown.bs.popover', function () {
        var tip = balao();
        if (!tip || tip.dataset.nexoConvite) return;
        tip.dataset.nexoConvite = '1';
        tip.addEventListener('mouseenter', function () { querAberto = true; window.clearTimeout(espera); });
        tip.addEventListener('mouseleave', esconderDepois);
      });
      gatilho.addEventListener('hidden.bs.popover', function () {
        if (querAberto) popover.show();
      });
      fechamentos.push(fechar);
    });
    document.addEventListener('keydown', function (evento) {
      if (evento.key !== 'Escape') return;
      fechamentos.forEach(function (fechar) { fechar(); });
    });
  }

  /* ---- Dropzone de vários arquivos ----
     A da base confere e mostra só o primeiro arquivo; aqui valem todos: extensão, quantidade e
     o total somado, que o servidor recusaria inteiro (413) depois do upload. */
  document.querySelectorAll('[data-dz-multiplo]').forEach(function (dz) {
    var input = dz.querySelector('[data-dz-input]');
    var rotulo = dz.querySelector('[data-dz-label]');
    var erro = dz.querySelector('[data-dz-erro]');
    var form = dz.closest('form');
    if (!input || !rotulo) return;
    var aceitos = (input.getAttribute('accept') || '').split(',')
      .map(function (s) { return s.trim().toLowerCase(); }).filter(Boolean);

    function limite(atributo) {
      return parseInt(dz.getAttribute(atributo) || '0', 10);
    }
    function megabytes(bytes) {
      return (bytes / 1048576).toFixed(1).replace('.', ',') + ' MB';
    }
    function problema() {
      var maxArquivos = limite('data-max-arquivos');
      var maxBytes = limite('data-max-bytes');
      var arquivos = input.files ? Array.prototype.slice.call(input.files) : [];
      if (maxArquivos && arquivos.length > maxArquivos) {
        return 'Selecione no máximo ' + maxArquivos + ' arquivos por envio.';
      }
      var recusado = arquivos.filter(function (arquivo) {
        var nome = String(arquivo.name).toLowerCase();
        return aceitos.length && !aceitos.some(function (ext) { return nome.slice(-ext.length) === ext; });
      })[0];
      if (recusado) return 'O arquivo “' + recusado.name + '” não está num formato aceito (' + aceitos.join(', ') + ').';
      var total = arquivos.reduce(function (soma, arquivo) { return soma + arquivo.size; }, 0);
      if (maxBytes && total > maxBytes) {
        return 'Os arquivos somam ' + megabytes(total) + '; o limite por envio é ' + megabytes(maxBytes) + '.';
      }
      return '';
    }
    function mostrarProblema(texto) {
      dz.classList.add('is-erro');
      if (erro) erro.textContent = texto;
    }
    function conferir() {
      var texto = problema();
      if (texto) {
        input.value = '';
        // A base limpa a seleção ao receber o change; o aviso vem depois, para não ser apagado.
        input.dispatchEvent(new Event('change'));
        mostrarProblema(texto);
        return;
      }
      var total = input.files ? input.files.length : 0;
      if (total > 1) rotulo.textContent = total + ' arquivos selecionados';
    }
    input.addEventListener('change', function () {
      if (input.files && input.files.length) conferir();
    });
    dz.addEventListener('drop', function () { window.setTimeout(conferir, 0); });

    if (!form) return;
    var botao = form.querySelector('button[type="submit"]');
    var textoBotao = botao ? botao.innerHTML : '';
    var liberar = null;
    function restaurar() {
      window.clearTimeout(liberar);
      if (!botao) return;
      botao.disabled = false;
      botao.removeAttribute('aria-busy');
      botao.innerHTML = textoBotao;
    }
    form.addEventListener('submit', function (evento) {
      if (evento.defaultPrevented) return;
      var texto = problema();
      if (texto) {
        evento.preventDefault();
        delete form.dataset.enviado;
        mostrarProblema(texto);
        return;
      }
      // A leitura pode levar até dois minutos: o botão fica ocupado até a página voltar. Se o
      // envio for interrompido (Esc, Parar), ele se solta depois do prazo do processamento.
      if (form.hasAttribute('data-envio-longo') && botao) {
        botao.disabled = true;
        botao.setAttribute('aria-busy', 'true');
        botao.textContent = 'Lendo os relatórios…';
        liberar = window.setTimeout(restaurar, 150000);
      }
    });
    window.addEventListener('pageshow', function (evento) {
      if (evento.persisted) restaurar();
    });
  });
})();
