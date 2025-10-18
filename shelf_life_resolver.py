# shelf_life_resolver.py  (DB-ONLY)
# -*- coding: utf-8 -*-
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.engine import Connection

from db import shelf_life_rules  # tablo şemanıza göre
# from db import storage  # API kullanmıyorsanız şart değil

logger = logging.getLogger(__name__)

OPEN_STATE_DEFAULT = "sealed"   # sealed|opened|cooked

class ShelfLifeResolver:
    """
    Sadece DB cache: ürün/kategori + storage + open_state -> days
    """
    def __init__(self):
        pass

    @staticmethod
    def _now():
        return datetime.now(timezone.utc)

    def _lookup_db(
        self,
        conn: Connection,
        product_id: Optional[int],
        category_id: Optional[int],
        storage_id: int,
        open_state: str
    ) -> Optional[int]:
        base = (
            select(shelf_life_rules.c.days)
            .where(shelf_life_rules.c.storage_id == storage_id)
            .where(shelf_life_rules.c.open_state == open_state)
        )
        # Önce ürün-kuralı (daha spesifik)
        if product_id is not None:
            row = conn.execute(base.where(shelf_life_rules.c.product_id == product_id).limit(1)).first()
            if row and row[0] is not None:
                return int(row[0])

        # Sonra kategori fallback
        if category_id is not None:
            row = conn.execute(base.where(shelf_life_rules.c.category_id == category_id).limit(1)).first()
            if row and row[0] is not None:
                return int(row[0])

        return None

    def resolve_days(
        self,
        conn: Connection,
        *,
        product_id: Optional[int],
        category_id: Optional[int],
        storage_id: int,
        product_name_for_api: str,         # API yok ama imza aynı kalsın
        open_state: str = OPEN_STATE_DEFAULT
    ) -> Optional[int]:
        return self._lookup_db(conn, product_id, category_id, storage_id, open_state)
