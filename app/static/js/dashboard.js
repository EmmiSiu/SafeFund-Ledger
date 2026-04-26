/**
 * dashboard.js — Alpine component + Chart.js for the BI dashboard.
 */
function dashboardApp(cajaId) {
  return {
    cajaId,
    chartLoading: true,
    chart: null,

    async init() {
      if (!this.cajaId) return;
      await this.loadChart();
    },

    async loadChart() {
      this.chartLoading = true;
      try {
        const res = await fetch(`/api/v1/dashboard/chart-data?caja_id=${this.cajaId}`);
        if (!res.ok) throw new Error('No se pudo cargar la gráfica');
        const data = await res.json();
        this.chartLoading = false;
        await this.$nextTick();
        this.renderChart(data);
      } catch (e) {
        this.chartLoading = false;
      }
    },

    renderChart({ labels, savings, interest }) {
      const canvas = document.getElementById('growthChart');
      if (!canvas) return;
      const ctx = canvas.getContext('2d');

      // Destroy previous instance on hot-reload
      if (this.chart) { this.chart.destroy(); }

      // Gradient fills
      const gradientSavings = ctx.createLinearGradient(0, 0, 0, 224);
      gradientSavings.addColorStop(0,   'rgba(52,211,153,0.25)');
      gradientSavings.addColorStop(1,   'rgba(52,211,153,0.02)');

      const gradientInterest = ctx.createLinearGradient(0, 0, 0, 224);
      gradientInterest.addColorStop(0,  'rgba(251,191,36,0.20)');
      gradientInterest.addColorStop(1,  'rgba(251,191,36,0.02)');

      const fmtMXN = (v) =>
        v.toLocaleString('es-MX', { style: 'currency', currency: 'MXN', maximumFractionDigits: 0 });

      this.chart = new Chart(ctx, {
        type: 'line',
        data: {
          labels,
          datasets: [
            {
              label: 'Ahorros acumulados',
              data: savings,
              borderColor: '#34d399',
              backgroundColor: gradientSavings,
              borderWidth: 2,
              pointRadius: 3,
              pointHoverRadius: 5,
              pointBackgroundColor: '#34d399',
              tension: 0.4,
              fill: true,
            },
            {
              label: 'Rendimiento acumulado',
              data: interest,
              borderColor: '#fbbf24',
              backgroundColor: gradientInterest,
              borderWidth: 2,
              pointRadius: 3,
              pointHoverRadius: 5,
              pointBackgroundColor: '#fbbf24',
              tension: 0.4,
              fill: true,
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { mode: 'index', intersect: false },
          plugins: {
            legend: { display: false },
            tooltip: {
              backgroundColor: '#1e293b',
              borderColor: '#334155',
              borderWidth: 1,
              padding: 12,
              titleColor: '#94a3b8',
              titleFont: { family: 'Inter', size: 11 },
              bodyColor: '#f1f5f9',
              bodyFont: { family: 'Inter', size: 13, weight: '600' },
              callbacks: {
                title: (items) => items[0].label,
                label: (item) => `  ${item.dataset.label}: ${fmtMXN(item.raw)}`,
              },
            },
          },
          scales: {
            x: {
              grid: { color: 'rgba(51,65,85,0.4)', drawBorder: false },
              ticks: { color: '#64748b', font: { family: 'Inter', size: 11 } },
            },
            y: {
              grid: { color: 'rgba(51,65,85,0.4)', drawBorder: false },
              ticks: {
                color: '#64748b',
                font: { family: 'Inter', size: 11 },
                callback: (v) => fmtMXN(v),
              },
            },
          },
        },
      });
    },
  };
}
