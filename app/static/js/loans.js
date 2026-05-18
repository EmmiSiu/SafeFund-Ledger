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
    allMembers:  initialMembers || [],   // includes member_type
    cajaId:      cajaId,
    cajaRates:   cajaRates || { internal: 0.05, external: 0.08 },
    search:      '',
    typeFilter:  '',

    get filtered() {
      let list = this.loans;
      if (this.search.trim()) {
        const q = this.search.toLowerCase();
        list = list.filter(l => l.member_name.toLowerCase().includes(q));
      }
      if (this.typeFilter) {
        list = list.filter(l => l.loan_type === this.typeFilter);
      }
      return list;
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
      member_id: '', member_name: '', loan_type: 'internal',
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

    // ── Init ──────────────────────────────────────────────────────────────────

    init() {
      this.loans = initialLoans.map(l => ({ ...l, _saving: false }));
      this.modal.date = new Date().toISOString().slice(0, 10);
      this.newLoan.interest_rate_pct = +(this.cajaRates.internal * 100).toFixed(2);
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

    onPaymentRegistered({ loanId, newBalance, fullyPaid }) {
      const loan = this.loans.find(l => l.id === loanId);
      if (!loan) return;
      loan.outstanding_balance = newBalance;
      loan.interest_paid_month = true;
      if (fullyPaid) {
        setTimeout(() => { this.loans = this.loans.filter(l => l.id !== loanId); }, 1000);
      }
    },

    onLoanDeleted({ loanId }) {
      this.loans = this.loans.filter(l => l.id !== loanId);
    },

    // ── New Loan — Member Search ──────────────────────────────────────────────

    openNewLoan() {
      this.newLoan = {
        open: true, loading: false, error: null,
        member_id: '', member_name: '', loan_type: 'internal',
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
      // Auto-detectar tipo de préstamo según tipo de socio
      this.newLoan.loan_type   = m.member_type === 'dentro' ? 'internal' : 'external';
      this.newLoan.interest_rate_pct = +(
        this.newLoan.loan_type === 'internal'
          ? this.cajaRates.internal * 100
          : this.cajaRates.external * 100
      ).toFixed(2);
      this.newLoan.memberSearch  = '';
      this.newLoan.memberResults = [];
    },

    clearMember() {
      this.newLoan.member_id   = '';
      this.newLoan.member_name = '';
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
        this.newLoan.open = false;
        window.__toast?.('Préstamo creado', 'success');
        setTimeout(() => window.location.reload(), 600);
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
    confirmDelete:    false,
    deleteLoading:    false,

    init() {
      this.form.amount = parseFloat(loanData.interes_mes.toFixed(2));
      this.calcBreakdown();
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
