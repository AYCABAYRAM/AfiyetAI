# db_normalizer.py
# -*- coding: utf-8 -*-
import logging
from typing import List, Dict, Optional, Tuple

from sqlalchemy import select, update                     # insert'ü buradan KALDIRDIK
from sqlalchemy.engine import Engine, Connection
from rapidfuzz import process, fuzz
import re
import unicodedata

from sqlalchemy.dialects.postgresql import insert as pg_insert  # <-- Postgres insert
from db import products, product_aliases, categories, product_translations
from translate_utils import translate_text
from sqlalchemy import join

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

logger = logging.getLogger(__name__)

# --- normalize helpers ---
UNIT_RE = re.compile(r'(?i)\b(\d+([.,]\d+)?\s*(kg|gr|g|ml|lt|l)|\d+\s*x\s*\d+\s*(g|ml))\b')
PAREN_RE = re.compile(r'\s*[\(\[\{].*?[\)\]\}]\s*')
NONWORD_RE = re.compile(r'[^0-9a-zA-ZğüşöçıİĞÜŞÖÇ\s]+')


class DBProductNormalizer:
    """
    DB tabanlı normalizer:
      - Eşleşme korpusu: product_aliases.alias_text + products.canonical_name_en
      - Skorlayıcı: rapidfuzz.fuzz.ratio
      - Eşik: 72 (ayarlanabilir)
    """

    def __init__(self, engine: Engine, fallback_normalizer=None, threshold: int = 72, scorer=fuzz.WRatio):
            self.engine = engine
            self.threshold = threshold
            self.fallback = fallback_normalizer
            self.scorer = scorer

            self._choices: List[str] = []
            self._choice_map: Dict[str, Tuple[Optional[int], Optional[int]]] = {}

    def _load_corpus(self, conn: Connection):
        self._choices.clear()
        self._choice_map.clear()

        # products
        for r in conn.execute(select(products.c.product_id, products.c.canonical_name_en, products.c.category_id)):
            name = self._clean_name(r.canonical_name_en or "")
            if name:
                self._choices.append(name)
                self._choice_map[name] = (r.product_id, r.category_id)

        # aliases + kategori (JOIN ile tek geçiş)
        j = join(product_aliases, products, product_aliases.c.product_id == products.c.product_id)
        q = select(
            product_aliases.c.alias_text,
            products.c.product_id,
            products.c.category_id
        ).select_from(j)

        for r in conn.execute(q):
            alias = self._clean_name(r.alias_text or "")
            if alias:
                self._choices.append(alias)
                self._choice_map[alias] = (r.product_id, r.category_id)

        logger.info("Normalizer corpus loaded: %d entries", len(self._choices))

    @staticmethod
    def _clean_name(name: str) -> str:
        """
        Ürün adı temizliği için TEK yetkili nokta.
        - TR güvenli küçük harf: casefold
        - Parantez içlerini, sayı+birim kalıplarını, noktalama/özel işaretleri temizler
        """
        s = (name or "").strip()
        s = unicodedata.normalize("NFKC", s)
        s = s.casefold()

        s = PAREN_RE.sub(" ", s)   # (aile boyu) vb.
        s = UNIT_RE.sub(" ", s)    # 500 gr, 2x500 ml vb.

        for suf in (" kg", " gr", " g", " ml", " lt", " l", " adet", " paket"):
            if s.endswith(suf):
                s = s[: -len(suf)].strip()

        s = NONWORD_RE.sub(" ", s)  # kalan noktalama vb.
        return " ".join(s.split())

    def match_one(self, conn: Connection, product_name: str) -> Dict:
        if not self._choices:
            self._load_corpus(conn)

        cleaned = self._clean_name(product_name)
        if not cleaned:
            return {"normalized_name": product_name, "product_id": None, "category_id": None, "score": 0}

        best = process.extractOne(cleaned, self._choices, scorer=fuzz.WRatio)

        if best:
            match_text, score, _ = best
            # dinamik eşik: kısa kelimelerde daha yüksek, uzunda biraz esnek
            L = len(cleaned)
            dyn_th = max(60, min(85, self.threshold + (8 - min(L, 8))))
            if score >= dyn_th:
                pid, cid = self._choice_map.get(match_text, (None, None))
                return {
                    "normalized_name": match_text,
                    "product_id": pid,
                    "category_id": cid,
                    "score": int(score),
                }

        if self.fallback:
            try:
                cat = self.fallback.categorize_product(product_name)
                norm = self.fallback.normalize_product_name(product_name)
                return {"normalized_name": norm, "product_id": None, "category_id": None, "score": 0, "category_hint": cat}
            except Exception:
                pass

        return {"normalized_name": cleaned, "product_id": None, "category_id": None, "score": 0}


        if product_translations:
            logger.info("PT columns -> %s", product_translations.c.keys())
        else:
            logger.info("PT table not found")

    def ensure_en_translation(self, conn, product_id: int, tr_name: str):
        if not product_id or not tr_name:
            return

        # 1) Bu ürün için EN çeviri zaten var mı? (target_lang='en' üzerinden kontrol)
        row = conn.execute(
            select(product_translations.c.translated_text)
            .where(product_translations.c.product_id == product_id)
            .where(product_translations.c.target_lang == "en")
            .limit(1)
        ).first()
        if row:
            return

        # 2) Çeviri (403 vb. olursa TR'yi geri kullan)
        try:
            en_name = translate_text(tr_name, source_lang="tr", target_lang="en") or tr_name
        except Exception as e:
            logger.warning("translate_text failed for product_id=%s: %s", product_id, e)
            en_name = tr_name

        # 3) UPSERT: ürün + hedef dil (EN) tekil olacak şekilde
        stmt = (
            pg_insert(product_translations)
            .values(
                product_id=product_id,
                source_lang="tr",
                source_text=tr_name.strip(),
                target_lang="en",
                translated_text=en_name.strip(),
                source="google",
            )
            .on_conflict_do_update(
                index_elements=["product_id", "source_lang", "target_lang", "source_text"],
                set_={
                    "translated_text": en_name.strip(),
                    "source": "google",
                },
            )
        )
        conn.execute(stmt)
        conn.commit()

    def upsert_alias(self, conn: Connection, alias_text: str, product_id: Optional[int], confidence: int = 60):
        alias = (alias_text or "").strip()
        if not alias or not product_id:
            return

        alias = unicodedata.normalize("NFKC", alias).casefold()
        try:
            confidence = int(confidence)
        except Exception:
            confidence = 60
        confidence = max(0, min(100, confidence))

        stmt = (
            pg_insert(product_aliases)
            .values(product_id=product_id, alias_text=alias, source="OCR", confidence=confidence)
            .on_conflict_do_nothing(index_elements=['alias_text'])   # DB'deki UNIQUE ile bire bir uyumlu olmalı
        )
        conn.execute(stmt)
