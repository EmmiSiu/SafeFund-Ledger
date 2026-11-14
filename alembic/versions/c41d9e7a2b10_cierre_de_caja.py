"""Cierre de caja: instrucciones, borrador, cierre, reparto y movimientos

Revision ID: c41d9e7a2b10
Revises: 7da6ac3a6359
Create Date: 2026-11-13

Solo agrega tablas nuevas; no modifica ninguna existente. Es idempotente porque
la app también crea tablas faltantes al arrancar (`Base.metadata.create_all`):
si ya existen, la migración las deja como están.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c41d9e7a2b10"
down_revision: Union[str, Sequence[str], None] = "7da6ac3a6359"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = (
    "cierre_movimientos",
    "cierre_socios",
    "cierres_caja",
    "cierre_borradores",
    "cierre_instrucciones",
)


def _existing() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _existing()

    if "cierre_instrucciones" not in existing:
        op.create_table(
            "cierre_instrucciones",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("caja_id", sa.Integer(), sa.ForeignKey("caja_config.id"), nullable=False),
            sa.Column("member_id", sa.Integer(), sa.ForeignKey("members.id"), nullable=False),
            sa.Column("instruccion", sa.String(20), nullable=False, server_default="por_definir"),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("caja_id", "member_id", name="uq_cierre_instr_caja_member"),
        )
        op.create_index("ix_cierre_instrucciones_caja_id", "cierre_instrucciones", ["caja_id"])

    if "cierre_borradores" not in existing:
        op.create_table(
            "cierre_borradores",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("caja_id", sa.Integer(), sa.ForeignKey("caja_config.id"), nullable=False, unique=True),
            sa.Column("fecha_corte", sa.Date(), nullable=True),
            sa.Column("factor_sin_prestamo", sa.Numeric(6, 4), nullable=False, server_default="0.5"),
            sa.Column("incluir_interes", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("overrides_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )

    if "cierres_caja" not in existing:
        op.create_table(
            "cierres_caja",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("caja_id", sa.Integer(), sa.ForeignKey("caja_config.id"), nullable=False, unique=True),
            sa.Column("fecha_corte", sa.Date(), nullable=False),
            sa.Column("idempotency_key", sa.String(64), nullable=False, unique=True),
            sa.Column("factor_sin_prestamo", sa.Numeric(6, 4), nullable=False),
            sa.Column("incluir_interes", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("total_ahorro", sa.Numeric(14, 2), nullable=False),
            sa.Column("total_rendimiento", sa.Numeric(14, 2), nullable=False),
            sa.Column("total_compensado", sa.Numeric(14, 2), nullable=False),
            sa.Column("total_a_entregar", sa.Numeric(14, 2), nullable=False),
            sa.Column("snapshot_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )

    if "cierre_socios" not in existing:
        op.create_table(
            "cierre_socios",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("cierre_id", sa.Integer(), sa.ForeignKey("cierres_caja.id"), nullable=False),
            sa.Column("member_id", sa.Integer(), sa.ForeignKey("members.id"), nullable=False),
            sa.Column("member_name", sa.String(150), nullable=False),
            sa.Column("ahorro_aportado", sa.Numeric(12, 2), nullable=False),
            sa.Column("pidio_prestamo", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("monto_desembolsado", sa.Numeric(12, 2), nullable=False),
            sa.Column("deuda_al_corte", sa.Numeric(12, 2), nullable=False),
            sa.Column("compensado", sa.Numeric(12, 2), nullable=False),
            sa.Column("ahorro_a_devolver", sa.Numeric(12, 2), nullable=False),
            sa.Column("factor", sa.Numeric(6, 4), nullable=False),
            sa.Column("porcentaje", sa.Numeric(10, 6), nullable=False),
            sa.Column("rendimiento", sa.Numeric(12, 2), nullable=False),
            sa.Column("total_a_entregar", sa.Numeric(12, 2), nullable=False),
            sa.UniqueConstraint("cierre_id", "member_id", name="uq_cierre_socio"),
        )
        op.create_index("ix_cierre_socios_cierre_id", "cierre_socios", ["cierre_id"])

    if "cierre_movimientos" not in existing:
        op.create_table(
            "cierre_movimientos",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("cierre_id", sa.Integer(), sa.ForeignKey("cierres_caja.id"), nullable=False),
            sa.Column("member_id", sa.Integer(), sa.ForeignKey("members.id"), nullable=False),
            sa.Column("loan_id", sa.Integer(), sa.ForeignKey("loans.id"), nullable=True),
            sa.Column("tipo", sa.String(20), nullable=False),
            sa.Column("monto", sa.Numeric(12, 2), nullable=False),
            sa.Column("transaction_id", sa.Integer(), sa.ForeignKey("transactions.id"), nullable=True),
            sa.Column("aportacion_id", sa.Integer(), sa.ForeignKey("aportaciones.id"), nullable=True),
        )
        op.create_index("ix_cierre_movimientos_cierre_id", "cierre_movimientos", ["cierre_id"])


def downgrade() -> None:
    existing = _existing()
    for table in TABLES:
        if table in existing:
            op.drop_table(table)
