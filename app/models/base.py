from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class CajaConfig(Base):
    __tablename__ = "caja_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    interest_rate_internal: Mapped[Decimal] = mapped_column(
        Numeric(5, 4), nullable=False, default=Decimal("0.0500")
    )
    interest_rate_external: Mapped[Decimal] = mapped_column(
        Numeric(5, 4), nullable=False, default=Decimal("0.0800")
    )
    total_quincenas: Mapped[int] = mapped_column(Integer, nullable=False, default=24)
    quota_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    # Fecha de inicio del ciclo — determina en qué quincena estamos hoy
    start_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    members: Mapped[list["Member"]] = relationship("Member", back_populates="caja")
    loans: Mapped[list["Loan"]] = relationship("Loan", back_populates="caja")
    groups: Mapped[list["MemberGroup"]] = relationship("MemberGroup", back_populates="caja", cascade="all, delete-orphan")
    aportaciones: Mapped[list["Aportacion"]] = relationship("Aportacion", back_populates="caja")


class MemberGroup(Base):
    __tablename__ = "member_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    caja_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("caja_config.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    caja: Mapped["CajaConfig"] = relationship("CajaConfig", back_populates="groups")
    members: Mapped[list["Member"]] = relationship("Member", back_populates="member_group")


class Member(Base):
    __tablename__ = "members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    group_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("member_groups.id"), nullable=False
    )
    caja_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("caja_config.id"), nullable=False
    )
    # "dentro" = socio interno | "fuera" = socio externo
    member_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="dentro", server_default="dentro"
    )
    # Cuota quincenal personal — lo que aporta cada quincena (puede diferir de caja.quota_amount)
    quota_personal: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00"), server_default="0.00"
    )
    # Tasa de interés mensual personalizada para préstamos a este socio.
    # None = usar la tasa por defecto de la caja según member_type (interna/externa).
    interest_rate: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(5, 4), nullable=True
    )
    # Saldo acumulado de ahorros quincenales
    savings_balance: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    caja: Mapped["CajaConfig"] = relationship("CajaConfig", back_populates="members")
    member_group: Mapped["MemberGroup"] = relationship("MemberGroup", back_populates="members")
    loans: Mapped[list["Loan"]] = relationship("Loan", back_populates="member")
    transactions: Mapped[list["Transaction"]] = relationship(
        "Transaction", back_populates="member"
    )
    aportaciones: Mapped[list["Aportacion"]] = relationship("Aportacion", back_populates="member")
    quincenas_pendientes: Mapped[list["QuincenaPendiente"]] = relationship("QuincenaPendiente", back_populates="member")


class Loan(Base):
    __tablename__ = "loans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    member_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("members.id"), nullable=False
    )
    caja_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("caja_config.id"), nullable=False
    )
    initial_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    # Saldo insoluto — única fuente de verdad para el cálculo de interés
    outstanding_balance: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False
    )
    interest_rate: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    # "internal" | "external"
    loan_type: Mapped[str] = mapped_column(String(20), nullable=False)
    # "active" | "paid" | "defaulted"
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    member: Mapped["Member"] = relationship("Member", back_populates="loans")
    caja: Mapped["CajaConfig"] = relationship("CajaConfig", back_populates="loans")
    transactions: Mapped[list["Transaction"]] = relationship(
        "Transaction", back_populates="loan"
    )


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        Index("ix_transactions_loan_type_date", "loan_id", "transaction_type", "transaction_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    # Nullable: las transacciones de tipo "savings" no pertenecen a un préstamo
    loan_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("loans.id"), nullable=True
    )
    member_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("members.id"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    transaction_date: Mapped[date] = mapped_column(Date, nullable=False)
    # "savings" | "interest_payment" | "capital_payment" | "loan_increment"
    transaction_type: Mapped[str] = mapped_column(String(30), nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    loan: Mapped[Optional["Loan"]] = relationship("Loan", back_populates="transactions")
    member: Mapped["Member"] = relationship(
        "Member", back_populates="transactions"
    )


class QuincenaPendiente(Base):
    """
    Registra qué cuotas quincenales están marcadas como pendientes (no pagadas).
    La ausencia de un registro implica que la quincena fue pagada en tiempo y forma.
    """

    __tablename__ = "quincenas_pendientes"
    __table_args__ = (
        Index("ix_qp_member_num", "member_id", "quincena_num", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    member_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("members.id"), nullable=False, index=True
    )
    caja_id: Mapped[int] = mapped_column(Integer, ForeignKey("caja_config.id"), nullable=False)
    quincena_num: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    member: Mapped["Member"] = relationship("Member", back_populates="quincenas_pendientes")


class Aportacion(Base):
    """Registro de aportaciones quincenales de los socios al capital de la caja."""

    __tablename__ = "aportaciones"
    __table_args__ = (
        Index("ix_aportaciones_member_fecha", "member_id", "fecha_aportacion"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    member_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("members.id"), nullable=False, index=True
    )
    caja_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("caja_config.id"), nullable=False
    )
    monto: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    fecha_aportacion: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    # Identificador de quincena: ej. "2026-01-Q1", "2026-01-Q2"
    quincena: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    notas: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    member: Mapped["Member"] = relationship("Member", back_populates="aportaciones")
    caja: Mapped["CajaConfig"] = relationship("CajaConfig", back_populates="aportaciones")


# ── Análisis y cierre de caja ──────────────────────────────────────────────────

class CierreInstruccion(Base):
    """Instrucción de pago elegida por socio antes del cierre."""

    __tablename__ = "cierre_instrucciones"
    __table_args__ = (
        UniqueConstraint("caja_id", "member_id", name="uq_cierre_instr_caja_member"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    caja_id: Mapped[int] = mapped_column(Integer, ForeignKey("caja_config.id"), nullable=False, index=True)
    member_id: Mapped[int] = mapped_column(Integer, ForeignKey("members.id"), nullable=False)
    # "ahorro" = liquidar con ahorro | "separado" = pago por separado | "por_definir"
    instruccion: Mapped[str] = mapped_column(String(20), nullable=False, default="por_definir")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CierreBorrador(Base):
    """Parámetros del cierre en preparación (uno por caja). Pantalla, PDF y cierre leen de aquí."""

    __tablename__ = "cierre_borradores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    caja_id: Mapped[int] = mapped_column(Integer, ForeignKey("caja_config.id"), nullable=False, unique=True)
    # None = usar la fecha de hoy
    fecha_corte: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    # Factor de ponderación para socios que nunca pidieron préstamo (los demás pesan 1)
    factor_sin_prestamo: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False, default=Decimal("0.5000"))
    incluir_interes: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # {"<member_id>": "<porcentaje>"} — porcentajes fijados a mano
    overrides_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CierreCaja(Base):
    """Corte confirmado de una caja. Inmutable: no se recalcula con correcciones posteriores."""

    __tablename__ = "cierres_caja"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Un solo cierre por caja (cada caja es un ciclo)
    caja_id: Mapped[int] = mapped_column(Integer, ForeignKey("caja_config.id"), nullable=False, unique=True)
    fecha_corte: Mapped[date] = mapped_column(Date, nullable=False)
    # Llave de idempotencia: reintentar la confirmación no duplica cargos ni pagos
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    factor_sin_prestamo: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False)
    incluir_interes: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    total_ahorro: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    total_rendimiento: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    total_compensado: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    total_a_entregar: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    # Copia congelada del análisis completo al momento del cierre
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    socios: Mapped[list["CierreSocio"]] = relationship(
        "CierreSocio", back_populates="cierre", cascade="all, delete-orphan"
    )
    movimientos: Mapped[list["CierreMovimiento"]] = relationship(
        "CierreMovimiento", back_populates="cierre", cascade="all, delete-orphan"
    )


class CierreSocio(Base):
    """Renglón del reparto por socio dentro de un cierre confirmado."""

    __tablename__ = "cierre_socios"
    __table_args__ = (
        UniqueConstraint("cierre_id", "member_id", name="uq_cierre_socio"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cierre_id: Mapped[int] = mapped_column(Integer, ForeignKey("cierres_caja.id"), nullable=False, index=True)
    member_id: Mapped[int] = mapped_column(Integer, ForeignKey("members.id"), nullable=False)
    member_name: Mapped[str] = mapped_column(String(150), nullable=False)
    ahorro_aportado: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    pidio_prestamo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    monto_desembolsado: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    deuda_al_corte: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    compensado: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    ahorro_a_devolver: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    factor: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False)
    porcentaje: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False)
    rendimiento: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    total_a_entregar: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    cierre: Mapped["CierreCaja"] = relationship("CierreCaja", back_populates="socios")


class CierreMovimiento(Base):
    """Bitácora de las compensaciones ahorro ↔ deuda aplicadas por un cierre."""

    __tablename__ = "cierre_movimientos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cierre_id: Mapped[int] = mapped_column(Integer, ForeignKey("cierres_caja.id"), nullable=False, index=True)
    member_id: Mapped[int] = mapped_column(Integer, ForeignKey("members.id"), nullable=False)
    loan_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("loans.id"), nullable=True)
    # "pago_interes" | "pago_capital" (lado deuda) | "cargo_ahorro" (lado ahorro)
    tipo: Mapped[str] = mapped_column(String(20), nullable=False)
    monto: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    transaction_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("transactions.id"), nullable=True)
    aportacion_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("aportaciones.id"), nullable=True)

    cierre: Mapped["CierreCaja"] = relationship("CierreCaja", back_populates="movimientos")
