/* Draws every <canvas data-kind="..."> on the page with Chart.js.
   Colours come from the active theme's CSS variables, so charts follow the
   theme switch; templates may still pass the old fixed hex colours, which are
   mapped to their meaning (high / medium / low risk, accent, ink). */
(function () {
  'use strict';
  var charts = [];
  var reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }
  function theme() {
    return {
      high: cssVar('--high'), medium: cssVar('--medium'), low: cssVar('--low'),
      accent: cssVar('--chart-accent'), primary: cssVar('--chart-primary'),
      ink: cssVar('--ink'), soft: cssVar('--ink-soft'), grid: cssVar('--grid'),
      surface: cssVar('--panel'), font: cssVar('--font-body') || 'sans-serif'
    };
  }
  var LEGACY = { '#A2402F': 'high', '#B9821F': 'medium', '#476B4C': 'low', '#A9793C': 'accent', '#1E2B45': 'primary' };
  function colour(c, t) {
    if (!c) return t.accent;
    var key = LEGACY[c.toUpperCase()];
    return key ? t[key] : c;
  }
  function withAlpha(c, a) {
    // Accepts #rrggbb; returns rgba() so gradients can fade out.
    var m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(c);
    if (!m) return c;
    return 'rgba(' + parseInt(m[1], 16) + ',' + parseInt(m[2], 16) + ',' + parseInt(m[3], 16) + ',' + a + ')';
  }

  // Total in the middle of each doughnut.
  var centreTotal = {
    id: 'centreTotal',
    afterDraw: function (chart) {
      if (chart.config.type !== 'doughnut') return;
      var meta = chart.getDatasetMeta(0);
      if (!meta.data.length) return;
      var t = theme(), ctx = chart.ctx, x = meta.data[0].x, y = meta.data[0].y;
      var total = chart.data.datasets[0].data.reduce(function (a, b) { return a + (+b || 0); }, 0);
      ctx.save();
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillStyle = t.ink; ctx.font = '600 26px ' + t.font;
      ctx.fillText(total, x, y - 7);
      ctx.fillStyle = t.soft; ctx.font = '500 11px ' + t.font;
      ctx.fillText(chart.canvas.dataset.centreLabel || 'enrollments', x, y + 14);
      ctx.restore();
    }
  };

  function baseOptions(t, legend) {
    return {
      responsive: true, maintainAspectRatio: false,
      animation: reduceMotion ? false : { duration: 900, easing: 'easeOutQuart' },
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: legend ? { position: 'bottom', labels: { usePointStyle: true, pointStyle: 'circle', boxWidth: 8, padding: 16, color: t.soft, font: { family: t.font, size: 12 } } } : { display: false },
        tooltip: {
          backgroundColor: t.ink, titleColor: t.surface, bodyColor: t.surface,
          titleFont: { family: t.font, weight: '600' }, bodyFont: { family: t.font },
          padding: 10, cornerRadius: 8, displayColors: true, boxPadding: 4
        }
      }
    };
  }
  function axes(t, maxY) {
    var tick = { color: t.soft, font: { family: t.font, size: 11 } };
    return {
      x: { grid: { display: false }, border: { display: false }, ticks: tick },
      y: { beginAtZero: true, max: maxY, grid: { color: t.grid }, border: { display: false }, ticks: tick }
    };
  }

  function draw(canvas) {
    var t = theme();
    var kind = canvas.dataset.kind;
    var labels = JSON.parse(canvas.dataset.labels || '[]');
    if (!labels.length) {
      canvas.parentElement.innerHTML = '<div class="chart-empty">Not enough data yet. This chart fills in as attendance and marks are recorded.</div>';
      return null;
    }
    var values = JSON.parse(canvas.dataset.values || '[]');

    if (kind === 'donut') {
      var colors = JSON.parse(canvas.dataset.colors || '[]').map(function (c) { return colour(c, t); });
      return new Chart(canvas, {
        type: 'doughnut', plugins: [centreTotal],
        data: { labels: labels, datasets: [{ data: values, backgroundColor: colors, borderColor: t.surface, borderWidth: 3, hoverOffset: 6, borderRadius: 4 }] },
        options: Object.assign(baseOptions(t, true), { cutout: '70%', interaction: { mode: 'nearest' } })
      });
    }
    if (kind === 'bar' || kind === 'multibar') {
      var series = kind === 'bar'
        ? [{ label: canvas.dataset.label || '', data: values, color: canvas.dataset.color }]
        : JSON.parse(canvas.dataset.series || '[]');
      var opts = baseOptions(t, kind === 'multibar');
      opts.scales = axes(t);
      return new Chart(canvas, {
        type: 'bar',
        data: {
          labels: labels,
          datasets: series.map(function (s) {
            var c = colour(s.color, t);
            return { label: s.label, data: s.data, backgroundColor: c, hoverBackgroundColor: withAlpha(c, 0.85),
                     borderRadius: 6, borderSkipped: false, maxBarThickness: 34, categoryPercentage: 0.7, barPercentage: 0.85 };
          })
        },
        options: opts
      });
    }
    if (kind === 'line') {
      var c = colour(canvas.dataset.color, t);
      var maxY = canvas.dataset.max ? parseFloat(canvas.dataset.max) : 100;
      var opts2 = baseOptions(t, false);
      opts2.scales = axes(t, maxY);
      return new Chart(canvas, {
        type: 'line',
        data: {
          labels: labels,
          datasets: [{
            label: canvas.dataset.label || '', data: values, borderColor: c, borderWidth: 2.5,
            tension: 0.35, cubicInterpolationMode: 'monotone', fill: true, pointRadius: 0, pointHoverRadius: 5,
            pointBackgroundColor: c, pointBorderColor: t.surface, pointBorderWidth: 2,
            backgroundColor: function (ctx) {
              var area = ctx.chart.chartArea;
              if (!area) return withAlpha(c, 0.15);
              var g = ctx.chart.ctx.createLinearGradient(0, area.top, 0, area.bottom);
              g.addColorStop(0, withAlpha(c, 0.28)); g.addColorStop(1, withAlpha(c, 0));
              return g;
            }
          }]
        },
        options: opts2
      });
    }
    return null;
  }

  function drawAll() {
    charts.forEach(function (ch) { ch.destroy(); });
    charts = [];
    document.querySelectorAll('canvas[data-kind]').forEach(function (canvas) {
      var ch = draw(canvas);
      if (ch) charts.push(ch);
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    if (window.Chart) {
      Chart.defaults.font.family = theme().font;
      drawAll();
    }
  });
  document.addEventListener('ua:themechange', function () {
    if (!window.Chart) return;
    Chart.defaults.font.family = theme().font;
    drawAll();
  });
})();
