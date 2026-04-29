from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.base import CajaConfig, Member
from app.schemas.members import MemberCreate, MemberRead

router = APIRouter(prefix="/members", tags=["Members"])


@router.get("/", response_model=list[MemberRead])
def list_members(caja_id: int = Query(...), db: Session = Depends(get_db)):
    return (
        db.query(Member)
        .filter(Member.caja_id == caja_id, Member.is_active == True)
        .order_by(Member.name)
        .all()
    )


@router.get("/{member_id}", response_model=MemberRead)
def get_member(member_id: int, db: Session = Depends(get_db)):
    member = db.get(Member, member_id)
    if not member:
        raise HTTPException(status_code=404, detail="Socio no encontrado.")
    return member


@router.post("/", response_model=MemberRead, status_code=status.HTTP_201_CREATED)
def create_member(payload: MemberCreate, db: Session = Depends(get_db)):
    if not db.get(CajaConfig, payload.caja_id):
        raise HTTPException(status_code=404, detail="Caja no encontrada.")
    
    from app.models.base import MemberGroup
    if not db.get(MemberGroup, payload.group_id):
        raise HTTPException(status_code=404, detail="Grupo no encontrado.")
    member = Member(**payload.model_dump())
    db.add(member)
    db.commit()
    db.refresh(member)
    return member
