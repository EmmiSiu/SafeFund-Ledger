from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String
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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    members: Mapped[list["Member"]] = relationship("Member", back_populates="caja")
    loans: Mapped[list["Loan"]] = relationship("Loan", back_populates="caja")


class Member(Base):
    __tablename__ = "members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    group: Mapped[str] = mapped_column(String(100), nullable=False)
    caja_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("caja_config.id"), nullable=False
    )
    # Saldo acumulado de ahorros quincenales
    savings_balance: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    caja: Mapped["CajaConfig"] = relationship("CajaConfig", back_populates="members")
    loans: Mapped[list["Loan"]] = relationship("Loan", back_populates="member")
    transactions: Mapped[list["Transaction"]] = relationship(
        "Transaction", back_populates="member"
    )


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
    # "savings" | "interest_payment" | "capital_payment"
    transaction_type: Mapped[str] = mapped_column(String(30), nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    loan: Mapped[Optional["Loan"]] = relationship("Loan", back_populates="transactions")
    member: Mapped["Member"] = relationship(
        "Member", back_populates="transactions"
    )
