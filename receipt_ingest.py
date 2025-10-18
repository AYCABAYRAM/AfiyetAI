# receipt_ingest.py
# -*- coding: utf-8 -*-
import cv2
import pytesseract
import numpy as np
from PIL import Image
import hashlib
import re
import io
import os
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Tuple
from sqlalchemy.engine import Engine
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from contextlib import contextmanager
from pathlib import Path  # <-- FIX 1: safer enhanced filename handling

from db import (
    get_engine,
    receipts,
    receipt_images,
    ocr_lines,
    receipt_items,
    inventory_batches,
    products,
    product_translations,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db_normalizer import DBProductNormalizer
from pattern_normalizer import PatternProductNormalizer
from shelf_life_resolver import ShelfLifeResolver
from product_normalizer_advanced import normalize_product_name

logger = logging.getLogger(__name__)

TESS_LANG = os.environ.get('OCR_LANG', 'tur+eng')


def _read_file_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _deskew(gray: np.ndarray) -> np.ndarray:
    # threshold → edges → HoughLines ile açı tahmini
    thr = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    edges = cv2.Canny(thr, 50, 150, apertureSize=3)
    lines = cv2.HoughLines(edges, 1, np.pi / 180, 120)
    angle_deg = 0.0
    if lines is not None and len(lines) > 0:
        # yataya yakın çizgilerin ortalamasını al
        angles = []
        for rho_theta in lines[:50]:
            for rho, theta in rho_theta:
                deg = (theta * 180 / np.pi)
                # sadece 0 veya 180 civarını dikkate al (metin satırları)
                if deg < 20 or deg > 160:
                    angles.append(deg if deg <= 90 else deg - 180)
        if angles:
            angle_deg = np.median(angles)
    if abs(angle_deg) > 0.5:
        (h, w) = gray.shape[:2]
        M = cv2.getRotationMatrix2D((w // 2, h // 2), angle_deg, 1.0)
        return cv2.warpAffine(
            gray, M, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE
        )
    return gray


def _enhance_for_ocr(bgr: np.ndarray) -> np.ndarray:
    """
    Fiş görüntüsünü OCR için optimize eder - ORİJİNAL VERSİYON
    Basit ve etkili preprocessing (resize ve blur yok)
    """
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    gray = _deskew(gray)

    # CLAHE ile kontrast
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    gray = clahe.apply(gray)

    # adaptif threshold (ışık dengesizliğine dayanıklı)
    # blockSize=41, C=11 → daha fazla metin yakalar
    thr = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 41, 11
    )

    # morfolojik temizlik (küçük gürültüleri temizle)
    kernel = np.ones((2, 2), np.uint8)
    opened = cv2.morphologyEx(thr, cv2.MORPH_OPEN, kernel, iterations=1)
    closed = cv2.morphologyEx(opened, cv2.MORPH_CLOSE, kernel, iterations=1)
    return closed


def pil_from_ndarray(arr: np.ndarray) -> Image.Image:
    return Image.fromarray(arr)


def _enhance_receipt_for_ocr(image_path: str) -> str:
    """OCR için görüntüyü iyileştir"""
    import cv2
    import numpy as np

    # Görüntüyü oku
    img = cv2.imread(image_path)
    if img is None:
        return image_path

    # Gri tonlamaya çevir
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # Kontrast artırma (CLAHE)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)

    # Gürültü azaltma
    denoised = cv2.medianBlur(enhanced, 3)

    # Kenar keskinleştirme
    kernel = np.array([[-1,-1,-1], [-1,9,-1], [-1,-1,-1]])
    sharpened = cv2.filter2D(denoised, -1, kernel)

    # Adaptive threshold
    thresh = cv2.adaptiveThreshold(
        sharpened, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY, 11, 2
    )

    # İyileştirilmiş görüntüyü kaydet
    p = Path(image_path)
    enhanced_path = str(p.with_name(f"{p.stem}_enhanced{p.suffix}"))  # <-- FIX 1 applied
    cv2.imwrite(enhanced_path, thresh)

    logger.info(f"🔧 OCR için görüntü iyileştirildi: {enhanced_path}")
    return enhanced_path


def _tess_configs() -> List[Tuple[str, str]]:
    # Fişler için optimize edilmiş OCR konfigürasyonları - ORİJİNAL VERSİYON
    # PSM 6: Tek kolon, satır odaklı (fişler için ideal)
    # PSM 4: Tek sütun sayfa (backup)
    # PSM 11: Sparse text (metin dağınıksa)
    # Whitelist yok → tüm karakterleri okuyabilir
    common = " -l {} --oem 3".format(TESS_LANG)
    return [
        (f"--psm 6{common}", "psm6"),  # tek kolon, satır odaklı
        (f"--psm 4{common}", "psm4"),  # tek sütun sayfa
        (f"--psm 11{common}", "psm11"),  # sparse text
    ]


def _clean_price(s: str) -> Optional[float]:
    # "3,70", "12.50", "12,50 TL" → 12.50
    if not s:
        return None
    s = s.strip().upper().replace("TL", "").replace("₺", "")
    s = s.replace(" ", "")
    # ondalık ayraç: virgül → nokta
    s = s.replace(",", ".")
    m = re.search(r"(\d+\.\d{1,2}|\d+)", s)
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None


def _clean_product_name(name: str) -> str:
    """
    Ürün adını temizle: miktar bilgilerini, sayıları, özel karakterleri ayıkla
    Bağlamı yakalayarak doğru ürün adını çıkar
    """
    if not name:
        return ""

    # Temel temizlik
    name = name.strip()

    # 🔍 BAĞLAM ANALİZİ - Özel durumlar

    # "Mİ YUMURTA 15 LI" -> "Yumurta"
    if re.search(r'\bM[İI]\s+YUMURTA\s+\d+\s*LI?\b', name, re.IGNORECASE):
        return "Yumurta"

    # "YUMURTA 15 LI" -> "Yumurta"
    if re.search(r'\bYUMURTA\s+\d+\s*LI\b', name, re.IGNORECASE):
        return "Yumurta"

    # "MEYVE NEK ŞETLİ 1LT" -> "Meyve Nektarı"
    if re.search(r'\bMEYVE\s+NEK\.?ŞETLİ\b', name, re.IGNORECASE):
        return "Meyve Nektarı"

    # "MEYVE NEK.ŞETLİ 1LT" -> "Meyve Nektarı"
    if re.search(r'\bMEYVE\s+NEK\.?ŞETLİ\b', name, re.IGNORECASE):
        return "Meyve Nektarı"

    # "UN 2 KG EFSANE" -> "Un"
    if re.search(r'\bUN\s+\d+\s*KG\b', name, re.IGNORECASE):
        return "Un"

    # "SÜZME PEYNİR" -> "Süzme Peynir"
    if re.search(r'\bSÜZME\s+PEYNİR\b', name, re.IGNORECASE):
        return "Süzme Peynir"

    # "ŞEKER KÜP" -> "Şeker Küp"
    if re.search(r'\bŞEKER\s+KÜP\b', name, re.IGNORECASE):
        return "Şeker Küp"

    # "TAVUK BAGET" -> "Tavuk Baget"
    if re.search(r'\bTAVUK\s+BAGET\b', name, re.IGNORECASE):
        return "Tavuk Baget"

    # "KEKİK" -> "Kekik"
    if re.search(r'\bKEKİK\b', name, re.IGNORECASE):
        return "Kekik"

    # "ZEYTİN SİYAH" -> "Zeytin"
    if re.search(r'\bZEYTİN\s+SİYAH\b', name, re.IGNORECASE):
        return "Zeytin"

    # "TURŞU KARIŞIK" -> "Turşu"
    if re.search(r'\bTURŞU\s+KARIŞIK\b', name, re.IGNORECASE):
        return "Turşu"

    # "REÇEL" -> "Reçel"
    if re.search(r'\bREÇEL\b', name, re.IGNORECASE):
        return "Reçel"

    # "KAKAO" -> "Kakao"
    if re.search(r'\bKAKAO\b', name, re.IGNORECASE):
        return "Kakao"

    # "PİRİNÇ" -> "Pirinç"
    if re.search(r'\bPİRİNÇ\b', name, re.IGNORECASE):
        return "Pirinç"

    # "TEL ŞEHRİYE" -> "Tel Şehriye"
    if re.search(r'\bTEL\s+ŞEHRİYE\b', name, re.IGNORECASE):
        return "Tel Şehriye"

    # "ARPA ŞEHRİYE" -> "Arpa Şehriye"
    if re.search(r'\bARPA\s+ŞEHRİYE\b', name, re.IGNORECASE):
        return "Arpa Şehriye"

    # "YUFKA" -> "Yufka"
    if re.search(r'\bYUFKA\b', name, re.IGNORECASE):
        return "Yufka"

    # "NANE" -> "Nane"
    if re.search(r'\bNANE\b', name, re.IGNORECASE):
        return "Nane"

    # "ŞEKER TOZ" -> "Şeker"
    if re.search(r'\bŞEKER\s+TOZ\b', name, re.IGNORECASE):
        return "Şeker"

    # "KRAKER" -> "Kraker"
    if re.search(r'\bKRAKER\b', name, re.IGNORECASE):
        return "Kraker"

    # "MAYA" -> "Maya"
    if re.search(r'\bMAYASI\b', name, re.IGNORECASE):
        return "Maya"

    # "SUT AROMALI" -> "Süt"
    if re.search(r'\bSUT\s+AROMALI\b', name, re.IGNORECASE):
        return "Süt"

    # Gıda dışı ürünleri filtrele
    non_food_keywords = [
        'PEÇETE', 'POŞET', 'PLASTIK', 'PLASTIC', 'BAG', 'POSET',
        'Remy', 'REMY',  # Marka/alkol
        'BARKARAMELLI4SGCANGA', 'BARKARAMELLİASGCANGA',  # OCR hatası
        'BLUME', 'DESTAN', 'EFSANE', 'ŞAFAK', 'İLKGÜN', 'DAPHNE',  # Markalar
    ]

    for keyword in non_food_keywords:
        if keyword in name.upper():
            return ""  # Gıda dışı ürünü filtrele

    # OCR hatalarını düzelt (minimal)
    name = name.replace("YUKKA", "YUFKA")
    name = name.replace("SOGR", "SOĞAN")

    # Sadece fiyatları ayıkla
    name = re.sub(r'\b\d+[.,]\d+\b', '', name)  # 163,50 gibi fiyatları ayıkla

    # Özel karakterleri boşluğa çevir (silme)
    name = re.sub(r'[xX*«»#,.\-]', ' ', name)

    # Çoklu boşlukları tek boşluğa çevir
    name = re.sub(r'\s+', ' ', name).strip()

    # Çok kısa isimleri filtrele ama çok katı olma
    if len(name) < 3:
        return ""

    return name


_PRODUCT_PATTERNS = [
    # 1) ÜRÜN ADI (%08): 3,70
    re.compile(
        r"(?P<name>.+?)\s*\(%\d+\)\s*[:\-]?\s*(?P<price>\d+[.,]\d+)\s*$",
        re.IGNORECASE,
    ),
    # 2) ÜRÜN ADI 3,70
    re.compile(r"(?P<name>.+?)\s+(?P<price>\d+[.,]\d+)\s*$", re.IGNORECASE),
    # 3) ağırlıklı: DOMATES 0,850KG x 45,90 = 39,02
    re.compile(
        r"(?P<name>.+?)\s+[\d.,]+\s*(KG|G|GR|LT|L)\s*[Xx]\s*[\d.,]+\s*=\s*(?P<price>\d+[.,]\d+)",
        re.IGNORECASE,
    ),
    # 4) ÜRÜN ADI 408 x14,95 (yeni format)
    re.compile(r"(?P<name>.+?)\s+\d+\s+[Xx](?P<price>\d+[.,]\d+)\s*$", re.IGNORECASE),
    # 5) ÜRÜN ADI 408 x2 ,00 (boşluklu format)
    re.compile(r"(?P<name>.+?)\s+\d+\s+[Xx](?P<price>\d+[.,]\s*\d+)\s*$", re.IGNORECASE),
    # 6) ÜRÜN ADI 408 *3,50 (yıldızlı format)
    re.compile(r"(?P<name>.+?)\s+\d+\s+\*(?P<price>\d+[.,]\d+)\s*$", re.IGNORECASE),
    # 7) ÜRÜN ADI 408 «3,50 (çift tırnak format)
    re.compile(r"(?P<name>.+?)\s+\d+\s+«(?P<price>\d+[.,]\d+)\s*$", re.IGNORECASE),
    # 8) ÜRÜN ADI 408 »4,45 (çift tırnak format)
    re.compile(r"(?P<name>.+?)\s+\d+\s+»(?P<price>\d+[.,]\d+)\s*$", re.IGNORECASE),
    # 9) ÜRÜN ADI #08 x1,15 (hash format)
    re.compile(r"(?P<name>.+?)\s+#\d+\s+[Xx](?P<price>\d+[.,]\d+)\s*$", re.IGNORECASE),
    # 10) ÜRÜN ADI %08 *6,99 (yüzde format)
    re.compile(r"(?P<name>.+?)\s+%\d+\s+\*(?P<price>\d+[.,]\d+)\s*$", re.IGNORECASE),
    # 11) ÜRÜN ADI %08 xİ,25 (yüzde x format)
    re.compile(r"(?P<name>.+?)\s+%\d+\s+[Xx](?P<price>\d+[.,]\d+)\s*$", re.IGNORECASE),
    # 12) ÜRÜN ADI 408 0,95 (basit format)
    re.compile(r"(?P<name>.+?)\s+\d{3}\s+(?P<price>\d+[.,]\d+)\s*$", re.IGNORECASE),
    # 13) ÜRÜN ADI 408 x2 ,00 (boşluklu x format)
    re.compile(r"(?P<name>.+?)\s+\d{3}\s+[Xx](?P<price>\d+[.,]\s*\d+)\s*$", re.IGNORECASE),
    # 14) ÜRÜN ADI 408 *3 99 (boşluklu yıldız format)
    re.compile(r"(?P<name>.+?)\s+\d{3}\s+\*(?P<price>\d+\s+\d+)\s*$", re.IGNORECASE),
    # 15) ÜRÜN ADI 408 x2, 29 (virgüllü format)
    re.compile(r"(?P<name>.+?)\s+\d{3}\s+[Xx](?P<price>\d+,\s*\d+)\s*$", re.IGNORECASE),
    # 16) ÜRÜN ADI 408 x2 ,00 (boşluklu virgül format)
    re.compile(r"(?P<name>.+?)\s+\d{3}\s+[Xx](?P<price>\d+\s*,\s*\d+)\s*$", re.IGNORECASE),
    # 17) ÜRÜN ADI 408 *3 99 (boşluklu yıldız format)
    re.compile(r"(?P<name>.+?)\s+\d{3}\s+\*(?P<price>\d+\s+\d+)\s*$", re.IGNORECASE),
    # 18) ÜRÜN ADI 408 «31,95 (çift tırnak format)
    re.compile(r"(?P<name>.+?)\s+\d{3}\s+«(?P<price>\d+[.,]\d+)\s*$", re.IGNORECASE),
    # 19) ÜRÜN ADI 408 x1, 00 (boşluklu format)
    re.compile(r"(?P<name>.+?)\s+\d{3}\s+[Xx](?P<price>\d+,\s+\d+)\s*$", re.IGNORECASE),
    # 20) ÜRÜN ADI 408 x2, 29 (virgüllü format)
    re.compile(r"(?P<name>.+?)\s+\d{3}\s+[Xx](?P<price>\d+,\d+)\s*$", re.IGNORECASE),
    # 21) ÜRÜN ADI 408 xİ,25 (özel karakter format)
    re.compile(r"(?P<name>.+?)\s+\d{3}\s+[Xx](?P<price>\d+[.,]\d+)\s*$", re.IGNORECASE),
    # 22) ÜRÜN ADI 408 x2, 29 (boşluklu virgül format)
    re.compile(r"(?P<name>.+?)\s+\d{3}\s+[Xx](?P<price>\d+,\s+\d+)\s*$", re.IGNORECASE),
    # 23) ÜRÜN ADI 408 x1, 00 (boşluklu format)
    re.compile(r"(?P<name>.+?)\s+\d{3}\s+[Xx](?P<price>\d+,\s+\d+)\s*$", re.IGNORECASE),
    # 24) ÜRÜN ADI 408 x2, 29 (virgüllü format)
    re.compile(r"(?P<name>.+?)\s+\d{3}\s+[Xx](?P<price>\d+,\d+)\s*$", re.IGNORECASE),
]

_SKIP_IF_CONTAINS = [
    # başlıklar / sabit metinler
    "FİŞ", "FIS", "FİŞ NO", "FIS NO", "FATURA", "FATURA NO",
    "MAĞAZA", "MAGAZA", "ŞUBE", "SUBE", "ADRES", "TEL", "TELEFON",
    "TARİH", "TARIH", "SAAT", "SIRA NO", "KASA", "KASİYER", "KASIYER",
    "MÜŞTERİ", "MUSTERI", "TC", "TCKN", "VKN", "VERGİ", "VERGI", "VERGİ DAİRESİ", "VERGI DAIRESI",

    # toplam/kdv/özet
    "ARA TOPLAM", "GENEL TOPLAM", "TOPLAM", "TOP. KDV", "KDV", "KDV TUTARI",
    "ÖDENEN", "ODENEN", "TOPLAM ÖDENEN", "KALAN", "ALINDI",

    # promosyon/indirim/puan
    "İNDİRİM", "INDIRIM", "PROMOSYON", "KAMPANYA", "KAMPANYALI",
    "PARA PUAN", "PARAPUAN", "PUAN", "KAZANILAN PUAN", "KULLANILAN PUAN", "TOPLAM PUAN",

    # ödeme/pos/banka
    "NAKİT", "NAKIT", "KREDİ KARTI", "KREDI KARTI", "BANKA KARTI",
    "POS", "EFT-POS", "BANKA", "VISA", "MASTERCARD", "İŞLEM NO", "ISLEM NO", "ONAY KODU", "MERCHANT",

    # iade/iptal/belge
    "İADE", "IADE", "İPTAL", "IPTAL", "DEĞİŞİM", "DEGISIM", "FİŞ İPTAL", "FIS IPTAL",

    # dipnot / teşekkür
    "TEŞEKKÜR", "TESEKKUR", "BEKLERİZ", "BEKLERIZ", "İADE VE DEĞİŞİM", "IADE VE DEGISIM",

    # diğer muhtemel başlıklar
    "BARKOD", "PLU", "ÜRÜN KODU", "URUN KODU", "AÇIKLAMA", "ACIKLAMA",
]


def _parse_product_line(text: str) -> Optional[Dict[str, str]]:
    """
    Satırı parse eder ve ürün adını temizler.
    ORİJİNAL VERSİYON: Basit ve etkili mantık
    """
    up = text.upper()
    
    # Skip kontrolü: Eğer skip keyword'ü varsa AMA fiyat yoksa skip et
    # Fiyat varsa skip etme! (ÖNEMLİ: Bu sayede "YUMURTA %08 *12,00" gibi satırları yakalıyoruz)
    if any(k in up for k in _SKIP_IF_CONTAINS) and not re.search(r"\d+[.,]\d{1,2}", up):
        return None

    # Pattern matching ile ürünleri çıkar
    for rx in _PRODUCT_PATTERNS:
        m = rx.search(text)
        if m:
            # Ham ürün adını al
            name_raw = m.group("name").strip()
            price = _clean_price(m.group("price"))

            if name_raw and (price is not None) and len(name_raw) >= 3:
                # Ürün adını temizle
                clean_name = _clean_product_name(name_raw)
                
                if clean_name and len(clean_name) >= 3:
                    return {"name": clean_name, "price": price}
    
    return None


@contextmanager
def _transaction(engine: Engine):
    conn = engine.connect()
    trans = conn.begin()
    try:
        yield conn
        if trans.is_active:
            trans.commit()
    except Exception:
        if trans.is_active:
            trans.rollback()
        raise
    finally:
        conn.close()


class ReceiptOCRIngestor:
    def __init__(
        self, engine: Engine, tesseract_path: Optional[str] = None, currency: Optional[str] = None
    ):
        if tesseract_path:
            pytesseract.pytesseract.tesseract_cmd = tesseract_path
        self.engine = engine
        self.currency = currency  # para kullanmıyoruz; None bırakmak OK

    def _ocr_variants(self, pil_img: Image.Image) -> List[Dict]:
        variants = []
        for cfg, tag in _tess_configs():
            try:
                data = pytesseract.image_to_data(
                    pil_img,
                    lang=TESS_LANG,
                    config=cfg,
                    output_type=pytesseract.Output.DICT,
                )
                # line aggregation
                lines = {}
                for i in range(len(data["text"])):
                    txt = data["text"][i]
                    conf = (
                        float(data["conf"][i])
                        if str(data["conf"][i]).isdigit()
                        else -1.0
                    )
                    ln = data["line_num"][i]
                    if not txt.strip():
                        continue
                    if ln not in lines:
                        lines[ln] = {"text": [], "confs": []}
                    lines[ln]["text"].append(txt)
                    lines[ln]["confs"].append(conf)
                merged = []
                for ln, obj in lines.items():
                    line_txt = " ".join(obj["text"]).strip()
                    if line_txt:
                        avg_conf = (
                            np.mean([c for c in obj["confs"] if c >= 0])
                            if obj["confs"]
                            else -1
                        )
                        merged.append(
                            {"line_no": ln, "text": line_txt, "avg_conf": float(avg_conf)}
                        )
                variants.append({"tag": tag, "lines": merged})
            except Exception as e:
                logger.warning(f"Tesseract failed for {tag}: {e}")
        return variants

    def _extract_products_from_lines(self, lines: List[str]) -> List[Dict]:
        products_list = []
        for ln in lines:
            parsed = _parse_product_line(ln)
            if parsed:
                products_list.append(
                    {"name": parsed["name"], "price": parsed["price"], "original_line": ln}
                )

        return products_list

    def process_and_persist(
        self, image_path: str, user_id: int, store_id: Optional[int] = None, purchase_date: Optional[datetime] = None
    ) -> Dict:
        """
        OCR → satır ayıklama → DB'ye yaz → normalize → stok partisi.
        Idempotent (aynı görsel 2 kez eklenmez).
        Toplam/KDV HESABI YOK.
        """
        if purchase_date is None:
            purchase_date = datetime.now(timezone.utc)

        raw_bytes = _read_file_bytes(image_path)
        img_hash = sha256_hex(raw_bytes)

        # 1) Görseli oku + iyileştir
        bgr = cv2.imdecode(np.frombuffer(raw_bytes, np.uint8), cv2.IMREAD_COLOR)
        if bgr is None:
            raise ValueError(f"Cannot read image: {image_path}")

        enhanced = _enhance_for_ocr(bgr)
        pil1 = pil_from_ndarray(enhanced)
        pil2 = Image.open(io.BytesIO(raw_bytes))  # orijinal

        # 2) OCR — çoklu konfig ile dene
        variants = []

        variants.extend(self._ocr_variants(pil1))  # iyileştirilmiş

        variants.extend(self._ocr_variants(pil2))  # orijinal

        # En iyi varyantı seç (ortalama satır güveni ve ürün sayısı metrikleriyle)
        best = None
        best_score = -1
        for v in variants:
            lines_sorted = sorted(v["lines"], key=lambda x: x["line_no"])
            raw_lines = [x["text"] for x in lines_sorted]
            prods = self._extract_products_from_lines(raw_lines)
            avg_conf = np.mean([x["avg_conf"] for x in lines_sorted]) if lines_sorted else -1
            score = (len(prods) * 3) + (avg_conf / 10.0)  # ürün sayısına ağırlık
            if score > best_score:
                best_score = score
                best = {"tag": v["tag"], "lines": lines_sorted, "products": prods}

        if not best or not best["products"]:

            logger.info("No products parsed from receipt.")

        # 3) DB’ye yaz (tek transaction) — TOPLAM/KDV YOK
        MAX_RETRIES = 2
        attempt = 0

        # örnek storage default: pantry (id=1). İsterseniz tabloya bakıp "pantry" çekebilirsiniz.
        PANTRY_ID = 1

        while True:
            try:
                with _transaction(self.engine) as conn:
                    # a) Aynı görsel daha önce eklenmiş mi?
                    dup = conn.execute(
                        select(receipt_images.c.image_id, receipt_images.c.receipt_id)
                        .where(receipt_images.c.hash_sha256 == img_hash)
                    ).first()
                    if dup:
                        rid = dup.receipt_id
                        items = conn.execute(
                            select(receipt_items).where(receipt_items.c.receipt_id == rid)
                        ).mappings().all()
                        return {
                            "success": True,
                            "message": "Duplicate image detected; returning existing receipt.",
                            "receipt_id": rid,
                            "products": [dict(x) for x in items],
                        }

                    # b) fiş kaydı (TOPLAM/CURRENCY set etmiyoruz)
                    rec_ins = receipts.insert().values(
                        user_id=user_id,
                        household_id=None,
                        store_id=store_id,
                        purchase_date=purchase_date,
                        total_amount=None,  # kullanılmıyor
                        currency=None,      # kullanılmıyor
                        ocr_engine="tesseract",
                        ocr_version=str(pytesseract.get_tesseract_version()),  # <-- str()
                        status="parsed",
                        image_path=image_path,
                    ).returning(receipts.c.receipt_id)
                    rid = conn.execute(rec_ins).scalar_one()

                    # c) receipt_images
                    conn.execute(
                        receipt_images.insert().values(
                            receipt_id=rid, file_path=image_path, hash_sha256=img_hash
                        )
                    )

                    # d) ocr_lines
                    if best:
                        for i, ln in enumerate(best["lines"], 1):
                            conn.execute(
                                ocr_lines.insert().values(
                                    receipt_id=rid,
                                    line_no=i,
                                    raw_text=ln["text"],
                                    ocr_confidence=round(ln["avg_conf"], 2)
                                    if ln["avg_conf"] is not None
                                    else None,
                                    block_type="line",
                                )
                            )

                    # e) receipt_items + normalize + inventory_batches
                    normalizer = DBProductNormalizer(self.engine)
                    resolver = ShelfLifeResolver()

                    parsed_products = best["products"] if best else []
                    for p in parsed_products:
                        # 1) Satırı ekle
                        ins_stmt = (
                            receipt_items.insert()
                            .values(
                                receipt_id=rid,
                                line_text=p["original_line"],
                                qty=None,
                                unit_id=None,
                                price=p.get("price"),
                                currency=None,
                                normalized_product_id=None,
                                normalized_variant_id=None,
                                normalization_confidence=None,
                                category_id=None,
                                extracted_price=p.get("price"),
                                is_manual_correction='N',  # char(1)
                            )
                            .returning(receipt_items.c.receipt_item_id)
                        )
                        item_id = conn.execute(ins_stmt).scalar_one()

                        # 2) Pattern-based Normalizasyon + DB fallback
                        pattern_normalizer = PatternProductNormalizer()
                        pattern_result = pattern_normalizer.normalize(p["name"])

                        normalized_name = None
                        confidence = 0.0
                        match = None  # <-- FIX 2: match her zaman tanımlı

                        if pattern_result and pattern_result.confidence >= 0.6:
                            normalized_name = pattern_result.normalized_name
                            confidence = pattern_result.confidence
                            logger.info(f"🎯 Pattern normalization: '{p['name']}' → '{normalized_name}' ({confidence:.2f})")
                        else:
                            # Fallback: DB normalizasyon
                            match = normalizer.match_one(conn, p["name"])
                            if match:
                                normalized_name = getattr(match, 'normalized_name', p["name"])
                                confidence = getattr(match, 'score', 0.5)
                                logger.info(f"🗄️ DB normalization: '{p['name']}' → '{normalized_name}' ({confidence:.2f})")
                            else:
                                normalized_name = p["name"].strip().title()
                                confidence = 0.3
                                logger.info(f"⚠️ Basic cleanup: '{p['name']}' → '{normalized_name}' ({confidence:.2f})")

                        # Çeviri işlemi
                        from translate_utils import translate_text
                        translated_name = translate_text(normalized_name, "tr", "en")

                        # Çeviriyi dosyaya kaydet (geçici)
                        if translated_name and translated_name != normalized_name:
                            with open("clean_translations.txt", "a", encoding="utf-8") as f:
                                f.write(f"{normalized_name} -> {translated_name}\n")

                        # 2.5) pid/cid/norm_name belirle (stok ve shelf-life için)
                        pid = None
                        cid = None
                        norm_name = normalized_name  # default
                        if match:
                            pid = getattr(match, "product_id", None)
                            cid = getattr(match, "category_id", None)
                            norm_name = getattr(match, "normalized_name", norm_name)

                        # Alias'ı gerçek pid ile güncelle (varsa)
                        normalizer.upsert_alias(conn, p["name"], pid)  # <-- FIX 3: None değil pid

                        # 3) receipt_items güncelle (sadece bilinen id'ler varsa)
                        if pid or cid:
                            conn.execute(
                                receipt_items.update()
                                .where(receipt_items.c.receipt_item_id == item_id)
                                .values(
                                    normalized_product_id=pid,
                                    category_id=cid,
                                    normalization_confidence=confidence,
                                )
                            )

                        # 5) Raf ömrü ve stok partisi
                        # varsayılan storage: ürünün defaultu varsa onu kullan, yoksa None
                        storage_id = None
                        if pid:
                            st = conn.execute(
                                select(products.c.default_storage_id)
                                .where(products.c.product_id == pid)
                            ).first()
                            if st and st.default_storage_id:
                                storage_id = int(st.default_storage_id)

                        days = resolver.resolve_days(
                            conn=conn,
                            product_id=pid,
                            category_id=cid,
                            storage_id=storage_id,
                            product_name_for_api=norm_name or p["name"],
                        )

                        expected_expiry_date = None
                        if isinstance(days, int) and days > 0:
                            expected_expiry_date = purchase_date.date() + timedelta(days=int(days))

                        # product_id NOT NULL → sadece pid varsa batch oluştur
                        if pid is not None:
                            conn.execute(
                                inventory_batches.insert().values(
                                    user_id=user_id,
                                    household_id=None,
                                    product_id=pid,
                                    variant_id=None,
                                    qty=1,
                                    unit_id=None,
                                    purchase_date=purchase_date.date(),
                                    storage_id=storage_id,  # NULL olabilir
                                    expected_expiry_date=expected_expiry_date,
                                    opened_at=None,
                                    status="in_stock",
                                    source="receipt",
                                )
                            )
                        else:
                            logger.info(
                                "No product match → inventory batch skipped for line: %r",
                                p["original_line"],
                            )

                    # Transaction başarıyla bitti
                    return {
                        "success": True,
                        "message": f"Receipt parsed and persisted ({len(best['products']) if best else 0} items).",
                        "receipt_id": rid,
                        "products": best["products"] if best else [],
                    }

            except OperationalError as e:
                attempt += 1
                logger.warning(f"DB operational error; retrying {attempt}/{MAX_RETRIES}: {e}")
                if attempt > MAX_RETRIES:
                    raise
            except Exception as e:
                logger.exception(f"Failed to persist receipt: {e}")
                raise


def process_receipt_image(image_path: str, user_id: int) -> Dict:
    """
    Fiş görselini işle ve sonuçları döndür
    Web arayüzü için wrapper fonksiyon
    """
    import time
    start_time = time.time()

    try:
        # ReceiptOCRIngestor'u kullan
        engine = get_engine()
        ingestor = ReceiptOCRIngestor(engine)

        # Fişi işle
        result = ingestor.process_and_persist(image_path, user_id=user_id)

        processing_time = time.time() - start_time

        # result Row/dict olabilir: güvenli kontrol
        success = False
        products = []
        try:
            if isinstance(result, dict):
                success = result.get("success", False)
                products = result.get("products", [])
            else:
                success = getattr(result, "success", False)
                products = getattr(result, "products", [])
        except Exception:
            success = False
            products = []

        if result and success:
            # Receipt ID'yi al
            receipt_id = None
            if isinstance(result, dict):
                receipt_id = result.get("receipt_id")
            else:
                receipt_id = getattr(result, "receipt_id", None)

            # Ürünleri formatla
            formatted_products = []
            logger.info(f"Toplam {len(products)} ürün bulundu")

            # Normalizer'ı başlat
            pattern_normalizer = PatternProductNormalizer()

            for i, product in enumerate(products):
                logger.info(f"Ürün {i+1}: {product}")

                # Veri yapısına göre alanları al
                if isinstance(product, dict):
                    line_text = product.get("line_text", "")
                else:
                    line_text = getattr(product, "line_text", "")

                # HAM METIN: OCR'dan gelen
                raw_name = line_text
                logger.info(f"🔍 HAM OCR metni: '{raw_name}'")

                # 🎯 GELİŞMİŞ NORMALİZASYON SİSTEMİ
                # 3 katmanlı: OCR Fix → Pattern Match → Fuzzy DB Match
                name, confidence = normalize_product_name(raw_name)
                
                logger.info(f"✅ NORMALİZE: '{raw_name}' → '{name}' (güven: {confidence:.2f})")

                # Gıda dışı ürünleri filtrele (her durumda çalışır)
                non_food_keywords = [
                    'PEÇETE', 'POŞET', 'PLASTIK', 'PLASTIC', 'BAG', 'POSET',
                    'REMY',  # Marka/alkol (büyük harf)
                    'BARKARAMELLI4SGCANGA', 'BARKARAMELLİASGCANGA',  # OCR hatası
                    'BLUME', 'DESTAN', 'EFSANE', 'ŞAFAK', 'İLKGÜN', 'DAPHNE',  # Markalar
                    'KART', 'CARD', 'İNDİRİM', 'DISCOUNT', 'PROMOSYON',
                    'NUMARA', 'NUMBER', 'ADRES', 'ADDRESS', 'TELEFON', 'PHONE',
                    'TARİH', 'DATE', 'SAAT', 'TIME', 'TOPLAM', 'TOTAL',
                    'KDV', 'TAX', 'VERGİ', 'PARA', 'MONEY', 'KREDİ', 'CREDIT'
                ]

                is_non_food = False
                for keyword in non_food_keywords:
                    if keyword in name.upper():
                        logger.info(f"🚫 Non-food item filtered: '{name}' (contains '{keyword}')")
                        is_non_food = True
                        break

                if is_non_food:
                    continue  # Bu ürünü atla, bir sonrakine geç

                # ⚠️ ÖNEMLİ: name zaten normalize edildi, tekrar temizleme YAPMA!
                # Sadece çok kısa (< 2 karakter) isimleri filtrele
                if len(name.strip()) < 2:
                    logger.warning(f"⚠️ Çok kısa ürün adı atlandı: '{name}'")
                    continue

                # Normalize edilmiş ürün ID'si varsa products tablosundan adı al
                name_en = ""
                
                # ⭐ İngilizce çeviri yap (normalize edilmiş isimden)
                try:
                    from translate_utils import translate_text
                    name_en = translate_text(name, source_lang="tr", target_lang="en")
                    if not name_en or len(name_en) < 2 or name_en == name:
                        name_en = name  # Çeviri başarısız olursa Türkçe ismini kullan
                    logger.info(f"🌐 Çeviri: '{name}' → '{name_en}'")
                except Exception as e:
                    logger.warning(f"⚠️ Çeviri hatası: {e}")
                    name_en = name  # Hata olursa Türkçe ismini kullan

                # Güvenli alan okuma (dict veya Row obje olabilir)
                def _safe_get(obj, key, default=None):
                    try:
                        if isinstance(obj, dict):
                            return obj.get(key, default)
                        return getattr(obj, key, default)
                    except Exception:
                        return default

                price = str(_safe_get(product, "price", ""))
                original_line = line_text

                # Eğer temizlenmiş isim çok kısaysa, original_line'dan yeni isim çıkar
                if len(name) < 3:
                    # Original line'dan temel ürün adını çıkar
                    alt_name = _clean_product_name(line_text)
                    if len(alt_name) > len(name):
                        name = alt_name

                # Raf ömrü bilgisi ekle (varsayılan değerler)
                shelf_life_days = None
                normalized_product_id = _safe_get(product, 'normalized_product_id', None)
                category_id = _safe_get(product, 'category_id', None)
                try:
                    if category_id is not None:
                        # SQL Decimal veya str olabilir, int'e çevir
                        category_id = int(category_id)
                except Exception:
                    category_id = None

                if normalized_product_id:
                    # Normalize edilmiş ürün için raf ömrü bilgisi al
                    try:
                        with engine.connect() as conn:
                            from sqlalchemy import text
                            result = conn.execute(text("""
                                SELECT slr.days
                                FROM shelf_life_rules slr
                                WHERE slr.product_id = :product_id
                                LIMIT 1
                            """), {"product_id": normalized_product_id})
                            row = result.first()
                            if row:
                                shelf_life_days = row.days
                    except Exception as e:
                        logger.warning(f"Raf ömrü bilgisi alınamadı: {e}")

                # Eğer raf ömrü bilgisi yoksa, kategori bazlı varsayılan değerler
                if not shelf_life_days:
                    # Kategori yoksa isimden tahmin et
                    if not category_id and name:
                        upper_name = name.upper()
                        if 'ZEYTİN' in upper_name:
                            category_id = 1
                        elif 'TAVUK' in upper_name or 'BAGET' in upper_name:
                            category_id = 3
                        elif 'KAKAO' in upper_name:
                            category_id = 5
                        elif 'PEYNİR' in upper_name:
                            category_id = 7
                        elif 'PİRİNÇ' in upper_name or 'ŞEHRİYE' in upper_name or 'YUFKA' in upper_name or 'MAKARNA' in upper_name:
                            category_id = 11
                        elif 'UN' in upper_name:
                            category_id = 18
                        elif 'ŞEKER' in upper_name:
                            category_id = 19
                        elif 'SÜT' in upper_name:
                            category_id = 20
                    if category_id:
                        # Kategori bazlı varsayılan raf ömrü
                        category_shelf_life = {
                            1: 30,   # Zeytin - 30 gün
                            3: 3,    # Tavuk - 3 gün
                            5: 365,  # Kakao - 1 yıl
                            7: 7,    # Peynir - 1 hafta
                            11: 30,  # Diğer - 30 gün
                            18: 365, # Un - 1 yıl
                            19: 365, # Şeker - 1 yıl
                            20: 3,   # Süt - 3 gün
                        }
                        shelf_life_days = category_shelf_life.get(category_id, 7)  # Varsayılan 7 gün
                        logger.info(f"🔍 Ürün: {name}, Kategori: {category_id}, Raf ömrü: {shelf_life_days} gün")
                    else:
                        shelf_life_days = 7  # Varsayılan 7 gün
                        logger.info(f"🔍 Kategori bilgisi yok, varsayılan raf ömrü: {shelf_life_days} gün")

                # ✅ name zaten normalize edildi (satır 834-839), TEKRAR YAPMA!
                logger.info(f"📤 Arayüze gönderilen ürün adı: '{name}' (original: '{line_text}')")

                formatted_products.append({
                    "name": name if name else "Ürün",
                    "normalized_text_tr": name if name else "Ürün",  # ⭐ Normalize edilmiş Türkçe isim
                    "name_tr": name if name else "Ürün",  # Fallback için
                    "name_en": name_en if name_en else name,  # ⭐ İngilizce çeviri (yukarıda yapıldı)
                    "normalized_text_en": name_en if name_en else name,  # Envanter için
                    "price": price,
                    "original_line": original_line,
                    "shelf_life_days": shelf_life_days,
                    "normalized_product_id": normalized_product_id,
                    "category_id": category_id
                })

            # result Row/dict olabilir: güvenli al
            rid = None
            try:
                if isinstance(result, dict):
                    rid = result.get("receipt_id")
                else:
                    rid = getattr(result, "receipt_id", None)
            except Exception:
                rid = None

            logger.info(f"✅ Receipt ID döndürülüyor: {rid}")
            return {
                "success": True,
                "products": formatted_products,
                "processing_time": processing_time,
                "receipt_id": rid
            }
        else:
            return {
                "success": False,
                "error": "Fiş işlenirken hata oluştu",
                "processing_time": processing_time
            }

    except Exception as e:
        logger.error(f"Fiş işleme hatası: {e}")
        return {
            "success": False,
            "error": str(e),
            "processing_time": time.time() - start_time
        }


if __name__ == "__main__":
    # Test için
    import sys
    if len(sys.argv) > 1:
        image_path = sys.argv[1]
        result = process_receipt_image(image_path)
        print(f"Sonuç: {result}")
    else:
        print("Kullanım: python receipt_ingest.py <image_path>")
