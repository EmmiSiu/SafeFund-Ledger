from app.api.v1.cajas import router as cajas_router
from app.api.v1.dashboard import router as dashboard_router
from app.api.v1.loans import router as loans_router
from app.api.v1.members import router as members_router
from app.api.v1.transactions import router as transactions_router

__all__ = [
    "cajas_router",
    "dashboard_router",
    "loans_router",
    "members_router",
    "transactions_router",
]
