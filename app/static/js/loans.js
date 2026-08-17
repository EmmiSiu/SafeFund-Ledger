/**
 * loans.js — Alpine.js + HTMX component for the loans page.
 *
 * Payment modes (quick modal from table row):
 *   'interest' — POST /api/v1/transactions/payment  (pre-filled with monthly interest)
 *   'capital'  — POST /api/v1/transactions/capital-reduction  (direct capital reduction)
 *
 * Gestionar drawer:
 *   Opens a HTMX-loaded slide-over with full historial + tri-mode payment form.
 *   After any payment, dispatches 'payment-registered' to update the table row
 *   and triggers HTMX refresh on #metrics-band.
 */
function loanManager(initialLoans, cajaId, initialMembers, cajaRates) {
  return {
    loans:       [],
    allMembers:  initialMembers || [],
    cajaId:      cajaId,
    cajaRates:   cajaRates || { internal: 0.05, external: 0.08 },
    search:      '',
    typeFilter:  '',
    dateFrom:    '',
    dateTo:      '',
    viewMode:    'grouped',   // 'individual' | 'grouped'
    expandedMembers: [],

    // ── Vista "Por Socio" — activos + liquidados, cronológico (server-driven) ──
    agrupado: [],
    agrupadoLoading: false,

    get agrupadoFiltered() {
      let groups = this.agrupado;
      if (this.search.trim()) {
        const q = this.search.toLowerCase();
        groups = groups.filter(g => g.member_name.toLowerCase().includes(q));
      }
      if (this.typeFilter || this.dateFrom || this.dateTo) {
        groups = groups
          .map(g => {
            let loans = g.loans;
            if (this.typeFilter) loans = loans.filter(l => l.loan_type === this.typeFilter);
            if (this.dateFrom) loans = loans.filter(l => l.start_date >= this.dateFrom);
            if (this.dateTo) loans = loans.filter(l => l.start_date <= this.dateTo);
            return { ...g, loans };
          })
          .filter(g => g.loans.length > 0);
      }
      return groups;
    },

    async loadAgrupado() {
      this.agrupadoLoading = true;
      try {
        const res = await fetch(`/api/v1/loans/agrupado-por-socio?caja_id=${this.cajaId}`);
        if (!res.ok) throw new Error('Error cargando préstamos por socio');
        this.agrupado = await res.json();
      } catch (e) {
        // Silencioso: no interrumpe el resto de la página si este endpoint falla.
        this.agrupado = [];
      } finally {
        this.agrupadoLoading = false;
      }
    },

    get filtered() {
      let list = this.loans;
      if (this.search.trim()) {
        const q = this.search.toLowerCase();
        list = list.filter(l => l.member_name.toLowerCase().includes(q));
      }
      if (this.typeFilter) {
        list = list.filter(l => l.loan_type === this.typeFilter);
      }
      if (this.dateFrom) {
        list = list.filter(l => l.start_date >= this.dateFrom);
      }
      if (this.dateTo) {
        list = list.filter(l => l.start_date <= this.dateTo);
      }
      return list;
    },

    get groupedMembers() {
      const map = {};
      for (const loan of this.filtered) {
        const mid = loan.member_id;
        if (!map[mid]) {
          map[mid] = {
            member_id: mid,
            member_name: loan.member_name,
            member_type: loan.member_type || 'dentro',
            group_name: loan.group_name || '',
            loans: [],
            totalInitial: 0,
            totalBalance: 0,
            totalInterest: 0,
          };
        }
        map[mid].loans.push(loan);
        map[mid].totalInitial  += parseFloat(loan.initial_amount);
        map[mid].totalBalance  += parseFloat(loan.outstanding_balance);
        map[mid].totalInterest += this.calcMonthlyInterest(loan);
      }
      return Object.values(map).sort((a, b) => b.totalBalance - a.totalBalance);
    },

    toggleMemberExpand(memberId) {
      const idx = this.expandedMembers.indexOf(memberId);
      if (idx >= 0) this.expandedMembers.splice(idx, 1);
      else this.expandedMembers.push(memberId);
    },

    // ── Analytics computed ─────────────────────────────────────────────────────

    get topDebtors() {
      return this.groupedMembers.slice(0, 10);
    },

    get topFrequency() {
      return [...this.groupedMembers]
        .sort((a, b) => b.loans.length - a.loans.length || b.totalInitial - a.totalInitial)
        .slice(0, 10)
        .map(g => ({ ...g, count: g.loans.length }));
    },

    get loanStats() {
      const all = this.filtered;
      const int_ = all.filter(l => l.loan_type === 'internal');
      const ext  = all.filter(l => l.loan_type === 'external');
      const totalBal = all.reduce((s, l) => s + parseFloat(l.outstanding_balance), 0);
      const intBal   = int_.reduce((s, l) => s + parseFloat(l.outstanding_balance), 0);
      const extBal   = ext.reduce((s, l) => s + parseFloat(l.outstanding_balance), 0);
      return {
        total: all.length,
        internal:  { count: int_.length, balance: intBal, pct: totalBal > 0 ? (intBal/totalBal*100).toFixed(1) : 0 },
        external:  { count: ext.length,  balance: extBal, pct: totalBal > 0 ? (extBal/totalBal*100).toFixed(1) : 0 },
        upToDate:  all.filter(l => l.interest_paid_month).length,
        pending:   all.filter(l => !l.interest_paid_month).length,
        totalMonthlyInterest: all.reduce((s, l) => s + this.calcMonthlyInterest(l), 0),
      };
    },

    // ── Quick Payment Modal ───────────────────────────────────────────────────

    modal: {
      open: false, loan: null, mode: 'interest',
      amount: 0, date: '', loading: false, error: null,
      breakdown: { interest: 0, capital: 0, newBalance: 0, overpayment: 0 },
    },

    // ── New Loan Modal ────────────────────────────────────────────────────────

    newLoan: {
      open: false, loading: false, error: null,
      member_id: '', member_name: '', group_name: '', loan_type: 'internal',
      initial_amount: 0, start_date: '',
      interest_rate_pct: 5,
      memberSearch: '', memberResults: [],
    },

    // ── Gestionar Drawer ──────────────────────────────────────────────────────

    gestionar: {
      open: false,
      loading: false,
      loanId: null,
    },

    // ── Tab Activos / Liquidados ─────────────────────────────────────────────

    activeTab: 'activos',  // 'activos' | 'liquidados'

    // ── Detector de Intereses Duplicados ─────────────────────────────────────

    duplicados: {
      open: false,
      loading: false,
      grupos: [],       // [{loan_id, member_id, member_name, loan_type, transacciones, dias_entre_pagos, total_monto, sugerido_conservar_id}]
      selected: [],     // ids de transacciones marcadas para purgar
      purging: false,
    },

    get duplicadosTotalTxns() {
      return this.duplicados.grupos.reduce((s, g) => s + g.transacciones.length, 0);
    },

    // ── Regularización Bulk ─────────────────────────────────────────────────

    regBulk: {
      open: false,
      notas: '',
      loading: false,
      excludeSearch: '',
      excludeResults: [],
      excludedLoans: [],      // [{loan_id, member_name, outstanding_balance, interes_mensual, loan_type}]
      searchLoading: false,
      searchAbort: null,
    },

    get regBulkPreview() {
      const today = new Date();
      const currentDay = today.getDate();
      const excludeIds = new Set(this.regBulk.excludedLoans.map(e => e.loan_id));
      const eligible = [];
      const noVencidos = [];

      for (const l of this.loans) {
        if (parseFloat(l.outstanding_balance) <= 0 || excludeIds.has(l.id)) continue;
        // Día de cobro = día del start_date
        const diaCobro = l.start_date ? parseInt(l.start_date.split('-')[2]) : 1;
        // Ajustar al último día del mes si el día no existe
        const lastDay = new Date(today.getFullYear(), today.getMonth() + 1, 0).getDate();
        const diaEfectivo = Math.min(diaCobro, lastDay);

        if (diaEfectivo > currentDay) {
          noVencidos.push({ ...l, diaCobro: diaEfectivo });
        } else {
          eligible.push({ ...l, diaCobro: diaEfectivo });
        }
      }

      const totalIntereses = eligible.reduce((s, l) => s + this.calcMonthlyInterest(l), 0);
      return { count: eligible.length, totalIntereses, noVencidos };
    },

    // ── Liquidados ──────────────────────────────────────────────────────────

    liquidados: [],
    liquidadosLoading: false,

    // ── Interés Extra Modal ─────────────────────────────────────────────────

    interesExtra: {
      open: false,
      loan: null,
      monto: 0,
      fechaPago: '',
      motivo: '',
      loading: false,
      error: null,
    },

    // ── Init ──────────────────────────────────────────────────────────────────

    init() {
      this.loans = initialLoans.map(l => ({ ...l, _saving: false }));
      this.modal.date = new Date().toISOString().slice(0, 10);
      this.newLoan.interest_rate_pct = +(this.cajaRates.internal * 100).toFixed(2);
      this.interesExtra.fechaPago = new Date().toISOString().slice(0, 10);
      this.loadDuplicados();
      this.loadAgrupado();
    },

    // ── Helpers ───────────────────────────────────────────────────────────────

    formatAmount(value) {
      const n = parseFloat(value ?? 0);
      return n.toLocaleString('es-MX', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    },

    paidPct(loan) {
      if (!loan || parseFloat(loan.initial_amount) === 0) return 0;
      const pct = (1 - parseFloat(loan.outstanding_balance) / parseFloat(loan.initial_amount)) * 100;
      return Math.min(100, Math.max(0, parseFloat(pct.toFixed(1))));
    },

    calcMonthlyInterest(loan) {
      if (!loan) return 0;
      return parseFloat(
        (parseFloat(loan.outstanding_balance) * parseFloat(loan.interest_rate)).toFixed(2)
      );
    },

    // Los préstamos de la vista "Por Socio" vienen del endpoint agrupado (campo
    // `loan_id`, no `id`). Los modales de pago necesitan el objeto real de
    // `this.loans` para la actualización optimista tras el pago.
    loanFromGrupo(loanInGroup) {
      return this.loans.find(l => l.id === loanInGroup.loan_id) || {
        id: loanInGroup.loan_id,
        member_name: '',
        outstanding_balance: loanInGroup.outstanding_balance,
        interest_rate: loanInGroup.interest_rate,
        interest_paid_month: loanInGroup.interest_paid_month,
      };
    },

    calcBreakdown() {
      if (!this.modal.loan || this.modal.amount <= 0 || this.modal.mode === 'capital') {
        this.modal.breakdown = { interest: 0, capital: 0, newBalance: 0, overpayment: 0 };
        return;
      }
      const balance  = parseFloat(this.modal.loan.outstanding_balance);
      const rate     = parseFloat(this.modal.loan.interest_rate);
      const payment  = parseFloat(this.modal.amount);

      const interestDue  = parseFloat((balance * rate).toFixed(2));
      const interestPaid = Math.min(payment, interestDue);
      const remainder    = parseFloat((payment - interestPaid).toFixed(2));
      const capitalPaid  = Math.min(remainder, balance);
      const overpayment  = parseFloat((remainder - capitalPaid).toFixed(2));
      const newBalance   = parseFloat((balance - capitalPaid).toFixed(2));

      this.modal.breakdown = {
        interest:    parseFloat(interestPaid.toFixed(2)),
        capital:     parseFloat(capitalPaid.toFixed(2)),
        newBalance:  parseFloat(newBalance.toFixed(2)),
        overpayment: parseFloat(overpayment.toFixed(2)),
      };
    },

    // ── Quick Modal ───────────────────────────────────────────────────────────

    openInterestModal(loan) {
      this.modal.mode    = 'interest';
      this.modal.loan    = loan;
      this.modal.amount  = this.calcMonthlyInterest(loan);
      this.modal.date    = new Date().toISOString().slice(0, 10);
      this.modal.error   = null;
      this.modal.loading = false;
      this.calcBreakdown();
      this.modal.open    = true;
    },

    openCapitalModal(loan) {
      this.modal.mode    = 'capital';
      this.modal.loan    = loan;
      this.modal.amount  = 0;
      this.modal.date    = new Date().toISOString().slice(0, 10);
      this.modal.error   = null;
      this.modal.loading = false;
      this.modal.breakdown = { interest: 0, capital: 0, newBalance: 0, overpayment: 0 };
      this.modal.open    = true;
    },

    closeModal() {
      this.modal.open  = false;
      this.modal.loan  = null;
      this.modal.error = null;
    },

    async submitPayment() {
      if (!this.modal.loan || this.modal.amount <= 0) return;
      if (this.modal.mode === 'capital') {
        await this._submitCapitalReduction();
      } else {
        await this._submitInterestPayment();
      }
    },

    async _submitInterestPayment() {
      const loanId    = this.modal.loan.id;
      const amount    = this.modal.amount;
      const payDate   = this.modal.date;
      const breakdown = { ...this.modal.breakdown };

      this.modal.loading = true;
      this.modal.error   = null;

      const loan        = this.loans.find(l => l.id === loanId);
      const prevBalance = loan.outstanding_balance;
      const prevStatus  = loan.interest_paid_month;
      loan.outstanding_balance = breakdown.newBalance;
      loan._saving = true;
      this.closeModal();

      try {
        const res = await fetch('/api/v1/transactions/payment', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ loan_id: loanId, amount, payment_date: payDate }),
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail ?? `Error ${res.status}`);
        }
        const data = await res.json();
        loan.outstanding_balance = data.new_outstanding_balance;
        loan.interest_paid_month = true;
        if (data.loan_fully_paid) {
          setTimeout(() => { this.loans = this.loans.filter(l => l.id !== loanId); }, 800);
        }
        // Refresh metrics band
        const mb = document.getElementById('metrics-band');
        if (mb) htmx.trigger(mb, 'refreshMetrics');
        this.loadAgrupado();
      } catch (err) {
        loan.outstanding_balance = prevBalance;
        loan.interest_paid_month = prevStatus;
        this.modal.loan    = loan;
        this.modal.amount  = amount;
        this.modal.date    = payDate;
        this.modal.mode    = 'interest';
        this.modal.error   = err.message;
        this.modal.loading = false;
        this.modal.open    = true;
        this.calcBreakdown();
      } finally {
        loan._saving = false;
      }
    },

    async _submitCapitalReduction() {
      const loanId  = this.modal.loan.id;
      const amount  = this.modal.amount;
      const payDate = this.modal.date;

      this.modal.loading = true;
      this.modal.error   = null;

      const loan        = this.loans.find(l => l.id === loanId);
      const prevBalance = loan.outstanding_balance;
      loan.outstanding_balance = Math.max(0, parseFloat(prevBalance) - amount);
      loan._saving = true;
      this.closeModal();

      try {
        const res = await fetch('/api/v1/transactions/capital-reduction', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ loan_id: loanId, amount, payment_date: payDate }),
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail ?? `Error ${res.status}`);
        }
        const data = await res.json();
        loan.outstanding_balance = data.new_outstanding_balance;
        if (data.loan_fully_paid) {
          setTimeout(() => { this.loans = this.loans.filter(l => l.id !== loanId); }, 800);
        }
        const mb = document.getElementById('metrics-band');
        if (mb) htmx.trigger(mb, 'refreshMetrics');
        this.loadAgrupado();
      } catch (err) {
        loan.outstanding_balance = prevBalance;
        this.modal.loan    = loan;
        this.modal.amount  = amount;
        this.modal.date    = payDate;
        this.modal.mode    = 'capital';
        this.modal.error   = err.message;
        this.modal.loading = false;
        this.modal.open    = true;
      } finally {
        loan._saving = false;
      }
    },

    // ── Gestionar Drawer ──────────────────────────────────────────────────────

    openGestionar(loan) {
      this.gestionar.loanId  = loan.id;
      this.gestionar.loading = true;
      this.gestionar.open    = true;

      // Configure HTMX on the content div and trigger load
      this.$nextTick(() => {
        const content = document.getElementById('gestionar-content');
        if (!content) return;
        const url = `/loans/${loan.id}/gestionar-partial`;
        content.setAttribute('hx-get', url);
        htmx.process(content);
        htmx.trigger(content, 'reloadGestionar');

        // Hide loading skeleton after HTMX loads
        content.addEventListener('htmx:afterSwap', () => {
          this.gestionar.loading = false;
        }, { once: true });
      });
    },

    onPaymentRegistered({ loanId, newBalance, fullyPaid, newInitialAmount }) {
      const loan = this.loans.find(l => l.id === loanId);
      if (!loan) return;
      loan.outstanding_balance = newBalance;
      if (newInitialAmount != null) loan.initial_amount = newInitialAmount;
      loan.interest_paid_month = true;
      if (fullyPaid) {
        setTimeout(() => { this.loans = this.loans.filter(l => l.id !== loanId); }, 1000);
      }
      // La vista "Por Socio" es server-driven — recargarla mantiene correcto el
      // estado liquidado/activo sin depender de mutar el arreglo local.
      this.loadAgrupado();
    },

    onLoanDeleted({ loanId }) {
      this.loans = this.loans.filter(l => l.id !== loanId);
      this.loadAgrupado();
    },

    // ── Regularización Bulk ─────────────────────────────────────────────────

    openRegBulk() {
      this.regBulk.open = true;
      this.regBulk.notas = '';
      this.regBulk.excludeSearch = '';
      this.regBulk.excludeResults = [];
      this.regBulk.excludedLoans = [];
      this.regBulk.loading = false;
    },

    async searchExclude() {
      const q = this.regBulk.excludeSearch.trim();
      if (q.length < 2) { this.regBulk.excludeResults = []; return; }

      if (this.regBulk.searchAbort) this.regBulk.searchAbort.abort();
      const ctrl = new AbortController();
      this.regBulk.searchAbort = ctrl;
      this.regBulk.searchLoading = true;

      try {
        const res = await fetch(
          `/api/v1/loans/miembros-con-prestamos?caja_id=${this.cajaId}&search=${encodeURIComponent(q)}`,
          { signal: ctrl.signal }
        );
        if (!res.ok) throw new Error('Error buscando');
        this.regBulk.excludeResults = await res.json();
      } catch (e) {
        if (e.name !== 'AbortError') this.regBulk.excludeResults = [];
      } finally {
        this.regBulk.searchLoading = false;
      }
    },

    toggleExcludeLoan(loan, memberName) {
      const idx = this.regBulk.excludedLoans.findIndex(e => e.loan_id === loan.loan_id);
      if (idx >= 0) {
        this.regBulk.excludedLoans.splice(idx, 1);
      } else {
        this.regBulk.excludedLoans.push({
          loan_id: loan.loan_id,
          member_name: memberName,
          outstanding_balance: loan.outstanding_balance,
          interes_mensual: loan.interes_mensual,
          loan_type: loan.loan_type,
        });
      }
    },

    isLoanExcluded(loanId) {
      return this.regBulk.excludedLoans.some(e => e.loan_id === loanId);
    },

    removeExcluded(loanId) {
      this.regBulk.excludedLoans = this.regBulk.excludedLoans.filter(e => e.loan_id !== loanId);
    },

    async submitRegBulk() {
      if (this.regBulk.loading) return;
      this.regBulk.loading = true;
      try {
        const res = await fetch('/api/v1/loans/regularizar-intereses-bulk', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            caja_id: this.cajaId,
            exclude_loan_ids: this.regBulk.excludedLoans.map(e => e.loan_id),
            notas: this.regBulk.notas || null,
          }),
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail ?? `Error ${res.status}`);
        }
        const data = await res.json();
        // Mark all regularized loans as paid this month
        const paidIds = new Set(data.detalles.map(d => d.loan_id));
        this.loans.forEach(l => {
          if (paidIds.has(l.id)) l.interest_paid_month = true;
        });
        this.regBulk.open = false;
        let msg = `${data.total_prestamos} préstamos regularizados — $${this.formatAmount(data.total_intereses)} en intereses`;
        if (data.no_vencidos && data.no_vencidos.length > 0) {
          msg += ` · ${data.no_vencidos.length} pendiente(s) sin vencer`;
        }
        window.__toast?.(msg, 'success');
        // Refresh metrics
        const mb = document.getElementById('metrics-band');
        if (mb) htmx.trigger(mb, 'refreshMetrics');
        this.loadAgrupado();
      } catch (err) {
        window.__toast?.(err.message, 'error');
      } finally {
        this.regBulk.loading = false;
      }
    },

    // ── Detector de Intereses Duplicados ─────────────────────────────────────

    async loadDuplicados() {
      this.duplicados.loading = true;
      try {
        const res = await fetch(`/api/v1/loans/duplicados-interes?caja_id=${this.cajaId}`);
        if (!res.ok) throw new Error('Error buscando duplicados');
        this.duplicados.grupos = await res.json();
        // Preseleccionar todo excepto la transacción sugerida a conservar (la más antigua) por grupo.
        this.duplicados.selected = this.duplicados.grupos.flatMap(g =>
          g.transacciones.filter(t => t.id !== g.sugerido_conservar_id).map(t => t.id)
        );
      } catch (e) {
        // Silencioso: no interrumpe la carga normal de la página si el detector falla.
        this.duplicados.grupos = [];
      } finally {
        this.duplicados.loading = false;
      }
    },

    isDuplicadoSelected(txnId) {
      return this.duplicados.selected.includes(txnId);
    },

    toggleDuplicadoSelection(txnId) {
      const idx = this.duplicados.selected.indexOf(txnId);
      if (idx >= 0) this.duplicados.selected.splice(idx, 1);
      else this.duplicados.selected.push(txnId);
    },

    get duplicadosSelectedTotal() {
      const ids = new Set(this.duplicados.selected);
      let total = 0;
      for (const g of this.duplicados.grupos) {
        for (const t of g.transacciones) {
          if (ids.has(t.id)) total += parseFloat(t.monto);
        }
      }
      return total;
    },

    async purgeSelectedDuplicados() {
      if (this.duplicados.purging || this.duplicados.selected.length === 0) return;
      this.duplicados.purging = true;
      const ids = [...this.duplicados.selected];
      let purged = 0;
      try {
        for (const id of ids) {
          const res = await fetch(`/api/v1/transactions/${id}`, { method: 'DELETE' });
          if (res.ok) purged++;
        }
        window.__toast?.(`${purged} movimiento(s) duplicado(s) purgado(s) — recargando…`, 'success');
        // Recarga completa: purgar interest_payment puede cambiar el estado
        // "Al corriente"/"Pendiente" de cada préstamo, que se calcula en el servidor.
        setTimeout(() => window.location.reload(), 700);
      } catch (err) {
        window.__toast?.(err.message, 'error');
        this.duplicados.purging = false;
      }
    },

    // ── Liquidados ──────────────────────────────────────────────────────────

    async switchTab(tab) {
      this.activeTab = tab;
      if (tab === 'liquidados' && this.liquidados.length === 0) {
        await this.loadLiquidados();
      }
    },

    async loadLiquidados() {
      this.liquidadosLoading = true;
      try {
        const res = await fetch(`/api/v1/loans/liquidados?caja_id=${this.cajaId}`);
        if (!res.ok) throw new Error('Error cargando liquidados');
        this.liquidados = await res.json();
      } catch (e) {
        window.__toast?.(e.message, 'error');
      } finally {
        this.liquidadosLoading = false;
      }
    },

    // ── Interés Extra ───────────────────────────────────────────────────────

    openInteresExtra(loan) {
      this.interesExtra = {
        open: true,
        loan,
        monto: 0,
        fechaPago: new Date().toISOString().slice(0, 10),
        motivo: '',
        loading: false,
        error: null,
      };
    },

    async submitInteresExtra() {
      if (this.interesExtra.loading || this.interesExtra.monto <= 0) return;
      if (!this.interesExtra.motivo || this.interesExtra.motivo.length < 5) {
        this.interesExtra.error = 'El motivo debe tener al menos 5 caracteres.';
        return;
      }
      this.interesExtra.loading = true;
      this.interesExtra.error = null;
      try {
        const res = await fetch(`/api/v1/loans/${this.interesExtra.loan.id}/interes-extra`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            monto: this.interesExtra.monto,
            fecha_pago: this.interesExtra.fechaPago,
            motivo: this.interesExtra.motivo,
          }),
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail ?? `Error ${res.status}`);
        }
        this.interesExtra.open = false;
        window.__toast?.('Interés extra registrado', 'success');
        // Reload liquidados to reflect new totals
        await this.loadLiquidados();
        // Refresh metrics
        const mb = document.getElementById('metrics-band');
        if (mb) htmx.trigger(mb, 'refreshMetrics');
        this.loadAgrupado();
      } catch (err) {
        this.interesExtra.error = err.message;
      } finally {
        this.interesExtra.loading = false;
      }
    },

    // ── New Loan — Member Search ──────────────────────────────────────────────

    openNewLoan() {
      this.newLoan = {
        open: true, loading: false, error: null,
        member_id: '', member_name: '', group_name: '', loan_type: 'internal',
        initial_amount: 0,
        start_date: new Date().toISOString().slice(0, 10),
        interest_rate_pct: +(this.cajaRates.internal * 100).toFixed(2),
        memberSearch: '', memberResults: [],
      };
    },

    filterMembersForLoan() {
      const q = this.newLoan.memberSearch.toLowerCase().trim();
      if (!q) { this.newLoan.memberResults = []; return; }
      this.newLoan.memberResults = this.allMembers
        .filter(m => m.name.toLowerCase().includes(q))
        .slice(0, 8);
    },

    selectMemberForLoan(m) {
      this.newLoan.member_id   = m.id;
      this.newLoan.member_name = m.name;
      this.newLoan.group_name  = m.group_name || 'Sin grupo';
      // Auto-detectar tipo de préstamo según tipo de socio
      this.newLoan.loan_type   = m.member_type === 'dentro' ? 'internal' : 'external';
      // Si el socio tiene una tasa personalizada registrada, usarla como default
      // (sigue siendo editable manualmente en el formulario).
      if (m.interest_rate != null) {
        this.newLoan.interest_rate_pct = +(m.interest_rate * 100).toFixed(2);
      } else {
        this.newLoan.interest_rate_pct = +(
          this.newLoan.loan_type === 'internal'
            ? this.cajaRates.internal * 100
            : this.cajaRates.external * 100
        ).toFixed(2);
      }
      this.newLoan.memberSearch  = '';
      this.newLoan.memberResults = [];
    },

    clearMember() {
      this.newLoan.member_id   = '';
      this.newLoan.member_name = '';
      this.newLoan.group_name  = '';
      this.newLoan.memberSearch  = '';
      this.newLoan.memberResults = [];
    },

    async submitNewLoan() {
      if (!this.newLoan.member_id || this.newLoan.initial_amount <= 0) return;
      this.newLoan.loading = true;
      this.newLoan.error   = null;
      try {
        const res = await fetch('/api/v1/loans/', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            member_id:      this.newLoan.member_id,
            caja_id:        this.cajaId,
            initial_amount: this.newLoan.initial_amount,
            loan_type:      this.newLoan.loan_type,
            start_date:     this.newLoan.start_date,
            interest_rate:  +(this.newLoan.interest_rate_pct / 100).toFixed(4),
          }),
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail ?? `Error ${res.status}`);
        }
        const data = await res.json();
        // Add new loan to table without full page reload
        const selMember = this.allMembers.find(m => m.id === this.newLoan.member_id);
        this.loans.push({
          ...data,
          member_id:    this.newLoan.member_id,
          member_name:  this.newLoan.member_name,
          member_type:  selMember?.member_type || 'dentro',
          group_name:   this.newLoan.group_name || 'Sin grupo',
          interest_paid_month: false,
          last_interest_date: null,
          _saving: false,
        });
        this.newLoan.open = false;
        window.__toast?.('Préstamo creado', 'success');
        // Refresh metrics band
        const mb = document.getElementById('metrics-band');
        if (mb) htmx.trigger(mb, 'refreshMetrics');
        this.loadAgrupado();
      } catch (err) {
        this.newLoan.error   = err.message;
        this.newLoan.loading = false;
      }
    },
  };
}

/**
 * gestionarPanel — Alpine component for the Gestionar drawer (HTMX partial).
 *
 * Defined here (not inline in the partial) so the function is already available
 * when HTMX swaps the partial into the DOM and Alpine evaluates x-data.
 */
function gestionarPanel(loanData, today) {
  return {
    loan: loanData,
    form: {
      mode: 'interest',
      amount: 0,
      date: today,
      notes: '',
      loading: false,
      error: null,
    },
    breakdown: { interest: 0, capital: 0, newBalance: 0, overpayment: 0 },
    editingStartDate: false,
    newStartDate:     loanData.start_date,
    startDateLoading: false,
    startDateError:   null,
    incrementing:     false,
    incrementAmount:  0,
    incrementNotes:   '',
    incrementLoading: false,
    incrementError:   null,
    editingTxnId:      null,
    editingTxnDate:    '',
    editingTxnLoading: false,
    deletingTxnId:     null,
    deletingTxnLoading: false,
    confirmDelete:    false,
    deleteLoading:    false,
    timeline: { open: false, loading: false, data: null },

    init() {
      this.form.amount = parseFloat(loanData.interes_mes.toFixed(2));
      this.calcBreakdown();
    },

    async toggleTimeline() {
      this.timeline.open = !this.timeline.open;
      if (this.timeline.open && !this.timeline.data) {
        this.timeline.loading = true;
        try {
          const res = await fetch(`/api/v1/loans/${this.loan.id}/timeline`);
          if (!res.ok) throw new Error('Error cargando línea de tiempo');
          this.timeline.data = await res.json();
        } catch (err) {
          window.__toast?.(err.message, 'error');
        } finally {
          this.timeline.loading = false;
        }
      }
    },

    fmt(v) {
      return parseFloat(v ?? 0).toLocaleString('es-MX', {
        minimumFractionDigits: 2, maximumFractionDigits: 2,
      });
    },

    modeDescription() {
      if (this.form.mode === 'interest') return 'Registra un pago de interés del mes. El saldo insoluto no se reduce.';
      if (this.form.mode === 'capital') return 'Abono directo al capital. Reduce el saldo insoluto sin cubrir intereses.';
      return 'Regla de Oro: el pago cubre primero el interés devengado del mes; el excedente reduce capital.';
    },

    calcBreakdown() {
      const balance  = parseFloat(this.loan.outstanding_balance);
      const rate     = parseFloat(this.loan.interest_rate);
      const payment  = parseFloat(this.form.amount || 0);

      if (payment <= 0) {
        this.breakdown = { interest: 0, capital: 0, newBalance: balance, overpayment: 0 };
        return;
      }

      if (this.form.mode === 'capital') {
        const cap  = Math.min(payment, balance);
        this.breakdown = { interest: 0, capital: cap, newBalance: Math.max(0, balance - cap), overpayment: Math.max(0, payment - cap) };
        return;
      }
      if (this.form.mode === 'interest') {
        this.breakdown = { interest: payment, capital: 0, newBalance: balance, overpayment: 0 };
        return;
      }
      // libre — Regla de Oro
      const intDue  = parseFloat((balance * rate).toFixed(2));
      const intPaid = Math.min(payment, intDue);
      const rem     = parseFloat((payment - intPaid).toFixed(2));
      const cap     = Math.min(rem, balance);
      const over    = parseFloat((rem - cap).toFixed(2));
      this.breakdown = {
        interest: parseFloat(intPaid.toFixed(2)),
        capital:  parseFloat(cap.toFixed(2)),
        newBalance: parseFloat((balance - cap).toFixed(2)),
        overpayment: over,
      };
    },

    async submitPayment() {
      if (this.form.amount <= 0 || this.form.loading) return;
      this.form.loading = true;
      this.form.error   = null;

      try {
        let url, body;
        if (this.form.mode === 'capital') {
          url  = '/api/v1/transactions/capital-reduction';
          body = {
            loan_id:      this.loan.id,
            amount:       this.form.amount,
            payment_date: this.form.date,
            notes:        this.form.notes || null,
          };
        } else {
          url  = '/api/v1/transactions/payment';
          body = {
            loan_id:      this.loan.id,
            amount:       this.form.amount,
            payment_date: this.form.date,
            notes:        this.form.notes || null,
          };
        }

        const res  = await fetch(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });

        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail ?? `Error ${res.status}`);
        }

        const data = await res.json();
        const newBalance = parseFloat(data.new_outstanding_balance ?? this.loan.outstanding_balance);
        const fullyPaid  = data.loan_fully_paid ?? false;

        // Notificar al padre (loanManager) vía evento
        window.dispatchEvent(new CustomEvent('payment-registered', {
          detail: { loanId: this.loan.id, newBalance, fullyPaid },
        }));

        // Recargar el panel via HTMX
        const content = document.getElementById('gestionar-content');
        if (content) {
          htmx.trigger(content, 'reloadGestionar');
        }
        // Recargar métricas
        const metrics = document.getElementById('metrics-band');
        if (metrics) {
          htmx.trigger(metrics, 'refreshMetrics');
        }

        window.__toast?.('Movimiento registrado', 'success');

      } catch (err) {
        this.form.error = err.message;
      } finally {
        this.form.loading = false;
      }
    },

    startEditTxnDate(txnId, currentDate) {
      this.editingTxnId   = txnId;
      this.editingTxnDate = currentDate;
    },

    cancelEditTxnDate() {
      this.editingTxnId   = null;
      this.editingTxnDate = '';
    },

    async submitEditTxnDate() {
      if (!this.editingTxnId || !this.editingTxnDate || this.editingTxnLoading) return;
      this.editingTxnLoading = true;
      try {
        const res = await fetch(`/api/v1/transactions/${this.editingTxnId}/fecha`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ transaction_date: this.editingTxnDate }),
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail ?? `Error ${res.status}`);
        }
        this.editingTxnId = null;
        this.editingTxnDate = '';
        // Reload the drawer to reflect new date + re-sorted historial
        const content = document.getElementById('gestionar-content');
        if (content) htmx.trigger(content, 'reloadGestionar');
        window.__toast?.('Fecha actualizada', 'success');
      } catch (err) {
        window.__toast?.(err.message, 'error');
      } finally {
        this.editingTxnLoading = false;
      }
    },

    async submitIncrement() {
      if (this.incrementAmount <= 0 || this.incrementLoading) return;
      this.incrementLoading = true;
      this.incrementError   = null;
      try {
        const res = await fetch(`/api/v1/loans/${this.loan.id}/incrementar`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            monto_adicional: this.incrementAmount,
            notas: this.incrementNotes || null,
          }),
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail ?? `Error ${res.status}`);
        }
        const data = await res.json();
        // Update parent table (balance + initial_amount for progress bar)
        window.dispatchEvent(new CustomEvent('payment-registered', {
          detail: {
            loanId: this.loan.id,
            newBalance: parseFloat(data.outstanding_balance),
            newInitialAmount: parseFloat(data.initial_amount),
            fullyPaid: false,
          },
        }));
        // Reload drawer + metrics
        const content = document.getElementById('gestionar-content');
        if (content) htmx.trigger(content, 'reloadGestionar');
        const metrics = document.getElementById('metrics-band');
        if (metrics) htmx.trigger(metrics, 'refreshMetrics');
        window.__toast?.('Préstamo incrementado', 'success');
      } catch (err) {
        this.incrementError = err.message;
      } finally {
        this.incrementLoading = false;
      }
    },

    async submitStartDate() {
      if (!this.newStartDate || this.startDateLoading) return;
      this.startDateLoading = true;
      this.startDateError   = null;
      try {
        const res = await fetch(`/api/v1/loans/${this.loan.id}/fecha-inicio`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ start_date: this.newStartDate }),
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail ?? `Error ${res.status}`);
        }
        this.loan.start_date   = this.newStartDate;
        this.editingStartDate  = false;
        // Recargar panel para actualizar interés acumulado
        const content = document.getElementById('gestionar-content');
        if (content) htmx.trigger(content, 'reloadGestionar');
        const metrics = document.getElementById('metrics-band');
        if (metrics) htmx.trigger(metrics, 'refreshMetrics');
        window.__toast?.('Fecha de inicio actualizada', 'success');
      } catch (err) {
        this.startDateError = err.message;
      } finally {
        this.startDateLoading = false;
      }
    },

    confirmDeleteTxn(txnId) {
      this.deletingTxnId      = txnId;
      this.deletingTxnLoading = false;
      // Cancel any date editing
      this.editingTxnId       = null;
      this.editingTxnDate     = '';
    },

    cancelDeleteTxn() {
      this.deletingTxnId      = null;
      this.deletingTxnLoading = false;
    },

    async submitDeleteTxn() {
      if (!this.deletingTxnId || this.deletingTxnLoading) return;
      this.deletingTxnLoading = true;
      try {
        const res = await fetch(`/api/v1/transactions/${this.deletingTxnId}`, {
          method: 'DELETE',
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail ?? `Error ${res.status}`);
        }
        const data = await res.json();
        this.deletingTxnId = null;

        // Update parent table with new loan state
        if (data.loan_id) {
          window.dispatchEvent(new CustomEvent('payment-registered', {
            detail: {
              loanId: data.loan_id,
              newBalance: data.loan_outstanding_balance,
              newInitialAmount: data.loan_initial_amount,
              fullyPaid: data.loan_status === 'paid',
            },
          }));
        }

        // Reload the drawer to reflect updated historial + KPIs
        const content = document.getElementById('gestionar-content');
        if (content) htmx.trigger(content, 'reloadGestionar');
        // Reload metrics band
        const metrics = document.getElementById('metrics-band');
        if (metrics) htmx.trigger(metrics, 'refreshMetrics');

        window.__toast?.('Movimiento eliminado', 'success');
      } catch (err) {
        window.__toast?.(err.message, 'error');
      } finally {
        this.deletingTxnLoading = false;
      }
    },

    async submitDelete() {
      if (this.deleteLoading) return;
      this.deleteLoading = true;
      try {
        const res = await fetch(`/api/v1/loans/${this.loan.id}`, { method: 'DELETE' });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail ?? `Error ${res.status}`);
        }
        window.dispatchEvent(new CustomEvent('loan-deleted', {
          detail: { loanId: this.loan.id },
        }));
        window.__toast?.('Préstamo eliminado', 'success');
        // Cerrar el panel
        window.dispatchEvent(new CustomEvent('close-gestionar'));
        const metrics = document.getElementById('metrics-band');
        if (metrics) htmx.trigger(metrics, 'refreshMetrics');
      } catch (err) {
        this.confirmDelete  = false;
        this.deleteLoading  = false;
        window.__toast?.(err.message, 'error');
      }
    },
  };
}
