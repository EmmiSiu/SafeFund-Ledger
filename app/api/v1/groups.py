from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.base import CajaConfig, MemberGroup, Member
from app.schemas.groups import GroupCreate, GroupRead

router = APIRouter(prefix="/groups", tags=["Groups"])

@router.post("/", response_model=GroupRead, status_code=status.HTTP_201_CREATED)
def create_group(payload: GroupCreate, db: Session = Depends(get_db)):
    caja = db.get(CajaConfig, payload.caja_id)
    if not caja:
        raise HTTPException(status_code=404, detail="Caja no encontrada.")
    
    existing_group = db.query(MemberGroup).filter(
        MemberGroup.name == payload.name,
        MemberGroup.caja_id == payload.caja_id
    ).first()
    
    if existing_group:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ya existe un grupo con este nombre en la caja.")
        
    group = MemberGroup(**payload.model_dump())
    db.add(group)
    db.commit()
    db.refresh(group)
    return group

@router.get("/caja/{caja_id}", response_model=list[GroupRead])
def get_groups_by_caja(caja_id: int, db: Session = Depends(get_db)):
    caja = db.get(CajaConfig, caja_id)
    if not caja:
        raise HTTPException(status_code=404, detail="Caja no encontrada.")
        
    return db.query(MemberGroup).filter(MemberGroup.caja_id == caja_id).all()

@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_group(group_id: int, db: Session = Depends(get_db)):
    group = db.get(MemberGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Grupo no encontrado.")
        
    # Check if there are members using this group name in this caja
    has_members = db.query(Member).filter(
        Member.group == group.name,
        Member.caja_id == group.caja_id
    ).first()
    
    if has_members:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail="No se puede eliminar el grupo porque hay socios asignados a él."
        )
        
    db.delete(group)
    db.commit()
    return None
