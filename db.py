# db.py
# -*- coding: utf-8 -*-
import os
from typing import Dict
from sqlalchemy import create_engine, MetaData, Table
from sqlalchemy.engine import Engine
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///afiyet_dev.db")

# Engine
_engine: Engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)

# Şemayı gerekiyorsa değiştir
SCHEMA = None  # SQLite için şema kullanma

# Meta + tüm tabloları yansıt
_metadata = MetaData(schema=SCHEMA)
_metadata.reflect(bind=_engine, schema=SCHEMA)

# İsim -> Table sözlüğü
tables: Dict[str, Table] = {t.name: t for t in _metadata.tables.values()}

# Sık kullanılanları modül değişkeni olarak export edelim
allergens                 = tables.get("allergens")
app_event_log             = tables.get("app_event_log")
categories                = tables.get("categories")
dietary_preferences       = tables.get("dietary_preferences")
households                = tables.get("households")
inventory_batches         = tables.get("inventory_batches")
ocr_lines                 = tables.get("ocr_lines")
product_aliases           = tables.get("product_aliases")
product_translations      = tables.get("product_translations")
products                  = tables.get("products")
receipt_images            = tables.get("receipt_images")
receipt_items             = tables.get("receipt_items")
receipts                  = tables.get("receipts")
recipe_recommendations    = tables.get("recipe_recommendations")
shelf_life_cache          = tables.get("shelf_life_cache")
shelf_life_rules          = tables.get("shelf_life_rules")
storage                   = tables.get("storage")
user_allergies            = tables.get("user_allergies")
user_dietary_preferences  = tables.get("user_dietary_preferences")
user_dislikes             = tables.get("user_dislikes")
user_households           = tables.get("user_households")
users                     = tables.get("users")

# Bazı kodlarda "storage_conditions" bekleniyorsa alias verelim:
storage_conditions = storage

def get_engine() -> Engine:
    return _engine

def get_table(name: str) -> Table:
    """İstediğin tabloyu sözlükten al. Yoksa KeyError atar."""
    try:
        return tables[name]
    except KeyError:
        raise KeyError(f"Table not found in schema '{SCHEMA}': {name}")
