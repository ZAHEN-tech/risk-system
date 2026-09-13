document.addEventListener('DOMContentLoaded', function () {
  var PALETTE = {
    high: '#A2402F',
    medium: '#B9821F',
    low: '#476B4C',
    brass: '#A9793C',
    ink: '#1E2B45'
  };
  var FONT = { family: "'IBM Plex Sans', sans-serif", size: 11 };

  document.querySelectorAll('canvas[data-kind]').forEach(function (canvas) {
    var kind = canvas.dataset.kind;
    var labels = JSON.parse(canvas.dataset.labels || '[]');

    if (!labels.length) {
      canvas.parentElement.innerHTML = '<div class="chart-empty">Not enough data yet.</div>';
      return;
    }

    if (kind === 'donut') {
      var values = JSON.parse(canvas.dataset.values || '[]');
      var colors = JSON.parse(canvas.dataset.colors || '[]');
      new Chart(canvas, {
        type: 'doughnut',
        data: { labels: labels, datasets: [{ data: values, backgroundColor: colors, borderWidth: 0 }] },
        options: {
          cutout: '62%',
          plugins: { legend: { position: 'bottom', labels: { boxWidth: 12, font: FONT } } }
        }
      });
    } else if (kind === 'bar') {
      var values = JSON.parse(canvas.dataset.values || '[]');
      var color = canvas.dataset.color || PALETTE.brass;
      var label = canvas.dataset.label || '';
      new Chart(canvas, {
        type: 'bar',
        data: { labels: labels, datasets: [{ label: label, data: values, backgroundColor: color, borderRadius: 3 }] },
        options: {
          plugins: { legend: { display: false } },
          scales: { y: { beginAtZero: true, ticks: { font: FONT } }, x: { ticks: { font: FONT } } }
        }
      });
    } else if (kind === 'multibar') {
      var series = JSON.parse(canvas.dataset.series || '[]');
      new Chart(canvas, {
        type: 'bar',
        data: {
          labels: labels,
          datasets: series.map(function (s) {
            return { label: s.label, data: s.data, backgroundColor: s.color, borderRadius: 3 };
          })
        },
        options: {
          plugins: { legend: { position: 'bottom', labels: { boxWidth: 12, font: FONT } } },
          scales: { y: { beginAtZero: true, ticks: { font: FONT } }, x: { ticks: { font: FONT } } }
        }
      });
    } else if (kind === 'line') {
      var values = JSON.parse(canvas.dataset.values || '[]');
      var color = canvas.dataset.color || PALETTE.brass;
      var label = canvas.dataset.label || '';
      var maxY = canvas.dataset.max ? parseFloat(canvas.dataset.max) : 100;
      new Chart(canvas, {
        type: 'line',
        data: {
          labels: labels,
          datasets: [{
            label: label, data: values, borderColor: color, backgroundColor: color,
            tension: 0.3, fill: false, pointRadius: 3, pointBackgroundColor: color
          }]
        },
        options: {
          plugins: { legend: { display: false } },
          scales: { y: { beginAtZero: true, max: maxY, ticks: { font: FONT } }, x: { ticks: { font: FONT } } }
        }
      });
    }
  });
});
