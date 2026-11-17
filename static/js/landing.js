/* COM-001: navegação e estrutura de avaliações independem do CDN de gráficos. */
(function () {
  'use strict';
  document.documentElement.classList.add('landing-enhanced');
  var toggle = document.getElementById('landing-menu-toggle');
  var navigation = document.getElementById('landing-navigation');
  if (toggle && navigation) {
    var mobile = window.matchMedia('(max-width: 992px)');
    function setMenu(open) {
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      navigation.hidden = mobile.matches && !open;
    }
    function syncMenu() {
      toggle.hidden = !mobile.matches;
      setMenu(false);
    }
    toggle.addEventListener('click', function () {
      setMenu(toggle.getAttribute('aria-expanded') !== 'true');
    });
    navigation.addEventListener('click', function (event) {
      if (event.target.closest('a')) setMenu(false);
    });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && toggle.getAttribute('aria-expanded') === 'true') {
        setMenu(false);
        toggle.focus();
      }
    });
    document.addEventListener('click', function (event) {
      var header = toggle.closest('.nexo-header');
      if (header && !header.contains(event.target)) setMenu(false);
    });
    if (mobile.addEventListener) mobile.addEventListener('change', syncMenu);
    else if (mobile.addListener) mobile.addListener(syncMenu);
    syncMenu();
  }

  function initReviews(root) {
    if (!root) return;
    if (root.nexoReviews) return root.nexoReviews.refresh();
    var previous = root.querySelector('[data-review-prev]');
    var next = root.querySelector('[data-review-next]');
    var indicators = root.querySelector('[data-review-indicators]');
    var status = root.querySelector('[data-review-status]');
    var empty = root.querySelector('[data-review-empty]');
    var controls = root.querySelector('.review-controls');
    var slides = [], index = 0;
    function show(selected) {
      if (!slides.length) return;
      index = (selected + slides.length) % slides.length;
      slides.forEach(function (slide, position) {
        slide.hidden = position !== index;
        slide.setAttribute('role', 'group');
        slide.setAttribute('aria-roledescription', 'depoimento');
        slide.setAttribute('aria-label', (position + 1) + ' de ' + slides.length);
      });
      if (indicators) Array.prototype.forEach.call(indicators.children, function (dot, position) {
        dot.setAttribute('aria-current', position === index ? 'true' : 'false');
      });
      if (status) status.textContent = 'Depoimento ' + (index + 1) + ' de ' + slides.length;
    }
    function refresh() {
      slides = Array.prototype.slice.call(root.querySelectorAll('[data-review-slide]'));
      index = Math.min(index, Math.max(0, slides.length - 1));
      if (empty) empty.hidden = slides.length > 0;
      [previous, next].forEach(function (button) {
        if (!button) return;
        button.hidden = slides.length < 2;
        button.disabled = slides.length < 2;
      });
      if (controls) controls.hidden = slides.length < 2;
      if (indicators) {
        indicators.replaceChildren();
        slides.forEach(function (_, position) {
          var dot = document.createElement('button');
          dot.type = 'button';
          dot.className = 'review-dot';
          dot.textContent = String(position + 1);
          dot.setAttribute('aria-label', 'Ir para depoimento ' + (position + 1));
          dot.addEventListener('click', function () { show(position); });
          indicators.appendChild(dot);
        });
        indicators.hidden = slides.length < 2;
      }
      if (slides.length) show(index);
      else if (status) status.textContent = '';
    }
    if (previous) previous.addEventListener('click', function () { show(index - 1); });
    if (next) next.addEventListener('click', function () { show(index + 1); });
    root.nexoReviews = { refresh: refresh };
    refresh();
    return root.nexoReviews;
  }
  window.NexoLanding = { initReviews: initReviews };
  document.querySelectorAll('[data-review-carousel]').forEach(initReviews);
})();

/* ============================================================
   NEXO — Landing Page · JS dos gráficos demonstrativos (Plotly)
   ============================================================
   Renderiza os 2 gráficos do painel da Demonstração:
     #demo-chart-evolucao   linha multissérie (5 meses de compras x vendas)
     #demo-chart-top        barra horizontal (top produtos do período)

   Tema espelha o design system NEXO (dark + paleta Hélio):
     - fundo transparente
     - tipografia Inter
     - paleta: roxo #a855f7, verde #22c55e, âmbar #f59e0b, info #38bdf8
     - tooltips dark com borda roxa

   Dados são FICTÍCIOS (uma loja de varejo demonstrativa, com produtos
   genéricos); os mesmos valores estão nas tabelas acessíveis do HTML.
   ============================================================ */

(function () {
  var rendered = false;
  function startCharts() {
  if (rendered || typeof window.Plotly === 'undefined') return;
  rendered = true;

  function drawChart(el, data, layout, config) {
    function fallback() {
      el.replaceChildren();
      el.removeAttribute('data-chart-ready');
      var note = document.querySelector('[data-chart-fallback-for="' + el.id + '"]');
      if (note) note.hidden = false;
    }
    try {
      Promise.resolve(window.Plotly.newPlot(el, data, layout, config)).then(function () {
        el.setAttribute('data-chart-ready', 'true');
        var note = document.querySelector('[data-chart-fallback-for="' + el.id + '"]');
        if (note) note.hidden = true;
      }, fallback);
    } catch (error) { fallback(); }
  }

  /* ---- Paleta NEXO (mesma do nexo.css) ---- */
  var NEXO = {
    purple:   '#a855f7',
    purpleD:  '#7c3aed',
    green:    '#22c55e',
    amber:    '#f59e0b',
    info:     '#38bdf8',
    text:     '#e8e9f0',
    textMut:  '#9aa0b7',
    textDim:  '#6a7088',
    grid:     'rgba(255,255,255,0.06)',
    surface:  '#181824'
  };

  /* ---- Layout base reaproveitado pelos 2 gráficos ---- */
  function baseLayout(extra) {
    return Object.assign({
      paper_bgcolor: 'rgba(0,0,0,0)',
      plot_bgcolor:  'rgba(0,0,0,0)',
      font: { family: 'Inter, sans-serif', color: NEXO.textMut, size: 11.5 },
      // Números no formato brasileiro: vírgula decimal e ponto no milhar (R$ 10.000).
      separators: ',.',
      margin: { l: 48, r: 16, t: 10, b: 36 },
      xaxis: { gridcolor: NEXO.grid, zerolinecolor: NEXO.grid, automargin: true, fixedrange: true },
      yaxis: { gridcolor: NEXO.grid, zerolinecolor: NEXO.grid, automargin: true, fixedrange: true },
      showlegend: false,
      hoverlabel: {
        bgcolor: NEXO.surface,
        bordercolor: NEXO.purple,
        font: { color: NEXO.text, family: 'Inter, sans-serif', size: 11.5 }
      }
    }, extra || {});
  }

  var baseConfig = {
    displayModeBar: false,
    responsive: true,
    scrollZoom: false,
    editable: false,
    staticPlot: false
  };

  /* ============================================================
     1) demo-chart-evolucao — linha multissérie de 5 meses
        Compras (laranja) vs Faturamento (azul neon)
     ============================================================ */
  (function () {
    var el = document.getElementById('demo-chart-evolucao');
    if (!el) return;
    var meses    = ['Dez/25', 'Jan/26', 'Fev/26', 'Mar/26', 'Abr/26'];
    var compras  = [138400, 124900, 149600, 152300, 156300];
    var vendas   = [132100, 119800, 138200, 141600, 142800];

    drawChart(el, [
      {
        type: 'scatter', mode: 'lines+markers',
        name: 'Compras',
        x: meses, y: compras,
        line: { color: NEXO.amber, width: 2.5, shape: 'spline', smoothing: 0.8 },
        marker: { color: NEXO.amber, size: 7, line: { color: '#0c0c14', width: 2 } },
        hovertemplate: '<b>%{x}</b><br>Compras: R$ %{y:,.0f}<extra></extra>'
      },
      {
        type: 'scatter', mode: 'lines+markers',
        name: 'Vendas',
        x: meses, y: vendas,
        line: { color: NEXO.info, width: 2.5, shape: 'spline', smoothing: 0.8 },
        marker: { color: NEXO.info, size: 7, line: { color: '#0c0c14', width: 2 } },
        // A faixa entre compras e vendas: o descasamento do mês, visível no próprio gráfico.
        fill: 'tonexty', fillcolor: 'rgba(245,158,11,0.10)',
        hovertemplate: '<b>%{x}</b><br>Vendas: R$ %{y:,.0f}<extra></extra>'
      }
    ], baseLayout({
      height: 220,
      showlegend: true,
      legend: {
        orientation: 'h', y: -0.18, x: 0.5, xanchor: 'center',
        font: { color: NEXO.textMut, size: 11 }
      },
      yaxis: { gridcolor: NEXO.grid, tickprefix: 'R$ ', tickformat: ',.0f', fixedrange: true, automargin: true },
      margin: { l: 60, r: 18, t: 14, b: 48 }
    }), Object.assign({}, baseConfig, { staticPlot: false }));
  })();

  /* ============================================================
     2) demo-chart-top — barra horizontal: top produtos do período
        (em faturamento R$)
     ============================================================ */
  // Nomes completos e curtos (telas até 640 px) dos produtos genéricos da demonstração.
  var PRODUTOS = [
    'Kit de ferramentas 12 peças',
    'Garrafa térmica inox 1 L',
    'Fone de ouvido Bluetooth',
    'Mochila escolar reforçada',
    'Lâmpada LED 9 W, caixa com 10'
  ];
  var PRODUTOS_CURTOS = ['Kit ferramentas', 'Garrafa térmica', 'Fone Bluetooth', 'Mochila escolar', 'Lâmpada LED'];

  (function () {
    var el = document.getElementById('demo-chart-top');
    if (!el) return;
    var produtos = PRODUTOS;
    var valores = [28450, 24800, 19200, 16500, 12700];

    drawChart(el, [{
      type: 'bar',
      orientation: 'h',
      x: valores,
      y: produtos,
      marker: {
        color: [NEXO.purple, NEXO.info, NEXO.amber, NEXO.green, NEXO.purpleD],
        line: { color: 'rgba(255,255,255,0.08)', width: 1 }
      },
      hovertemplate: '<b>%{y}</b><br>R$ %{x:,.0f}<extra></extra>'
    }], baseLayout({
      height: 220,
      margin: { l: window.innerWidth <= 640 ? 110 : 170, r: 20, t: 14, b: 34 },
      xaxis: { gridcolor: NEXO.grid, tickprefix: 'R$ ', tickformat: ',.0f', fixedrange: true, automargin: true },
      yaxis: { autorange: 'reversed', tickfont: { size: 11, color: NEXO.textMut }, fixedrange: true, automargin: true, tickvals: produtos, ticktext: window.innerWidth <= 640 ? PRODUTOS_CURTOS : produtos }
    }), Object.assign({}, baseConfig, { staticPlot: false }));
  })();
  var resizeQueued = false;
  window.addEventListener('resize', function () {
    if (resizeQueued) return;
    resizeQueued = true;
    window.requestAnimationFrame(function () {
      resizeQueued = false;
      var chart = document.getElementById('demo-chart-top');
      if (!chart || !chart.hasAttribute('data-chart-ready')) return;
      var compact = window.innerWidth <= 640;
      try {
        Promise.resolve(window.Plotly.relayout(chart, {
          'margin.l': compact ? 110 : 170,
          'yaxis.ticktext': compact ? PRODUTOS_CURTOS : PRODUTOS
        })).catch(function () {});
      } catch (error) { /* A tabela acessível permanece disponível. */ }
    });
  });
  }
  var loader = document.getElementById('nexo-plotly');
  if (typeof window.Plotly !== 'undefined') startCharts();
  else if (loader) loader.addEventListener('load', startCharts, { once: true });
})();
