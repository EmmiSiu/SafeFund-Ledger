/**
 * loans.js — AlpineJS component for the active loans table.
 * Supports two payment modes:
 *   'interest' — POST /api/v1/transactions/payment  (Regla de Oro, amount pre-filled)
 *   'capital'  — POST /api/v1/transactions/capital-reduction  (direct capital reduction)
 * Both modes support a date picker and Optimistic UI with rollback on error.
 */
function loanManager(initialLoans, cajaId, initialMembers, cajaRates) {
  return {
    loans: [],
    members: initialMembers || [],
    cajaId: cajaId,
    cajaRates: cajaRates || { internal: 0.05, external: 0.08 },
    search: '',

    get filtered() {
      if (!this.search.trim()) return this.loans;
      const q = this.search.toLowerCase();
      return this.loans.filter(l => l.member_name.toLowerCase().includes(q));
    },

    modal: {
      open: false,
      loan: null,
      mode: 'interest',   // 'interest' | 'capital'
      amount: 0,
      date: '',
      loading: false,
      error: null,
      breakdown: { interest: 0, capital: 0, newBalance: 0, overpayment: 0 },
    },

    newLoan: {
      open: false, loading: false, error: null,
      member_id: '', initial_amount: 0, loan_type: 'internal',
      start_date: '', interest_rate_pct: 5,
    },

    init() {
      this.loans = initialLoans.map(l => ({ ...l, _saving: false }));
      this.modal.date = new Date().toISOString().slice(0, 10);
      this.newLoan.interest_rate_pct = +(this.cajaRates.internal * 100).toFixed(2);
    },

    // ── Helpers ──────────────────────────────────────────────────

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

    // Live breakdown — mirrors finance_engine.apply_payment logic in JS
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

    // ── Modal ────────────────────────────────────────────────────

    openInterestModal(loan) {
      this.modal.mode      = 'interest';
      this.modal.loan      = loan;
      this.modal.amount    = this.calcMonthlyInterest(loan);
      this.modal.date      = new Date().toISOString().slice(0, 10);
      this.modal.error     = null;
      this.modal.loading   = false;
      this.calcBreakdown();
      this.modal.open      = true;
    },

    openCapitalModal(loan) {
      this.modal.mode      = 'capital';
      this.modal.loan      = loan;
      this.modal.amount    = 0;
      this.modal.date      = new Date().toISOString().slice(0, 10);
      this.modal.error     = null;
      this.modal.loading   = false;
      this.modal.breakdown = { interest: 0, capital: 0, newBalance: 0, overpayment: 0 };
      this.modal.open      = true;
    },

    closeModal() {
      this.modal.open  = false;
      this.modal.loan  = null;
      this.modal.error = null;
    },

    // ── Submit dispatcher ────────────────────────────────────────

    async submitPayment() {
      if (!this.modal.loan || this.modal.amount <= 0) return;
      if (this.modal.mode === 'capital') {
        await this._submitCapitalReduction();
      } else {
        await this._submitInterestPayment();
      }
    },

    // ── Interest payment (Regla de Oro) ─────────────────────────

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
      loan._saving             = true;

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

    // ── New Loan ─────────────────────────────────────────────────

    openNewLoan() {
      this.newLoan = {
        open: true, loading: false, error: null,
        member_id: '', initial_amount: 0, loan_type: 'internal',
        start_date: new Date().toISOString().slice(0, 10),
        interest_rate_pct: +(this.cajaRates.internal * 100).toFixed(2),
      };
    },

    async submitNewLoan() {
      if (!this.newLoan.member_id || this.newLoan.initial_amount <= 0) return;
      this.newLoan.loading = true;
      this.newLoan.error = null;
      try {
        const res = await fetch('/api/v1/loans/', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            member_id:     this.newLoan.member_id,
            caja_id:       this.cajaId,
            initial_amount: this.newLoan.initial_amount,
            loan_type:     this.newLoan.loan_type,
            start_date:    this.newLoan.start_date,
            interest_rate: +(this.newLoan.interest_rate_pct / 100).toFixed(4),
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
        this.newLoan.error = err.message;
        this.newLoan.loading = false;
      }
    },

    // ── Capital reduction (direct) ───────────────────────────────

    async _submitCapitalReduction() {
      const loanId    = this.modal.loan.id;
      const amount    = this.modal.amount;
      const payDate   = this.modal.date;

      this.modal.loading = true;
      this.modal.error   = null;

      const loan        = this.loans.find(l => l.id === loanId);
      const prevBalance = loan.outstanding_balance;
      const optimisticBalance = Math.max(0, parseFloat(prevBalance) - amount);
      loan.outstanding_balance = optimisticBalance;
      loan._saving             = true;

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
  };
}
