"""
🎯 ADVANCED PRODUCT NORMALIZER
Sürdürülebilir, efektif, 3 katmanlı normalizasyon sistemi

Katman 1: OCR Hata Düzeltme
Katman 2: Akıllı Pattern Tanıma  
Katman 3: Fuzzy Database Matching
"""

import re
import logging
from typing import Optional, Dict, Tuple
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)


class ProductNormalizerAdvanced:
    """Gelişmiş ürün ismi normalizasyonu"""
    
    def __init__(self):
        # Katman 1: OCR Hata Dictionary
        self.ocr_fixes = {
            # Türkçe karakter hataları
            'I': 'İ', 'i': 'ı',
            # Sayı-harf karışımları
            '0': 'O', '1': 'I', '5': 'S', '8': 'B',
            # Yaygın OCR hataları
            'STY': 'Sı', 'STYAH': 'Siyah',
            'SUT': 'Süt', 'SUTY': 'Süt Y',
            'CAY': 'Çay', 'CANGA': 'Karamelli',
            'BAR': '', 'FLZ': '',  # Marka ön ekleri
        }
        
        # Katman 2: Ürün Pattern Dictionary
        self.product_patterns = {
            # Süt ürünleri
            r'S[UÜİI]+T.*?Y?AR[IİI]+M.*?YA[GĞ]+L[IİI]+': 'Süt Yarım Yağlı',
            r'S[UÜİI]+T.*?TAM.*?YA[GĞ]+L[IİI]+': 'Süt Tam Yağlı',
            r'S[UÜİI]+T': 'Süt',
            r'S[UÜ]+ZME.*?PEYN[IİI]+R': 'Süzme Peynir',
            r'PEYN[IİI]+R': 'Peynir',
            
            # Yağlar
            r'OMEGA\s*[0-9]*\s*YA[GĞ][IİI]': 'Omega 3 Yağı',
            r'ZEYT[IİI]N\s*YA[GĞ][IİI]': 'Zeytinyağı',
            r'AY[CÇ][IİI]CEK\s*YA[GĞ][IİI]': 'Ayçiçek Yağı',
            r'YA[GĞ]': 'Yağ',
            
            # Çaylar
            r'S[IİI]+Y?AH.*?[CÇ]AY': 'Siyah Çay',
            r'YE[SŞ]+[IİI]+L.*?[CÇ]AY': 'Yeşil Çay',
            r'[CÇ]AY': 'Çay',
            
            # Şekerler
            r'[SŞ]+EKER.*?K[UÜ]+P': 'Küp Şeker',
            r'[SŞ]+EKER.*?TOZ': 'Toz Şeker',
            r'[SŞ]+EKER': 'Şeker',
            
            # Makarnalar
            r'BONCUK\s*MAKARNA': 'Boncuk Makarna',
            r'TEL\s*[SŞ]EHR[IİI]YE': 'Tel Şehriye',
            r'ARPA\s*[SŞ]EHR[Iİİ]YE': 'Arpa Şehriye',
            r'[SŞ]EHR[Iİİ]YE': 'Şehriye',
            r'MAKARNA': 'Makarna',
            
            # Baharatlar ve diğer
            r'PUL\s*B[IİI]+BER': 'Pul Biber',
            r'B[IİI]+BER': 'Biber',
            r'KEK[IİI]+K': 'Kekik',
            r'NANE': 'Nane',
            r'K[IİI]+M[IİI]+ON': 'Kimyon',
            r'KARAMEL+[IİI]+': 'Karamelli Bar',
            
            # Turşu/Konserve
            r'TUR[SŞ]U\s*KAR[IİI][SŞ][IİI]K': 'Karışık Turşu',
            r'TUR[SŞ]U': 'Turşu',
            
            # Tohumlar
            r'AY[CÇ]+EK[IİI]+RDE[GĞ]+[IİI]+': 'Ayçekirdeği',
            r'[CÇ]+EK[IİI]+RDEK': 'Çekirdek',
            
            # Yumurta
            r'YUMURTA': 'Yumurta',
            
            # Pirinç/Un
            r'P[IİI]LAV\s*L[IİI]K': 'Pilavlık Pirinç',
            r'P[IİI]R[IİI]N[CÇ]': 'Pirinç',
            r'UN': 'Un',
            
            # Tuz
            r'TUZ.*?[YT]+OTLU': 'Otlu Tuz',
            r'TUZ': 'Tuz',
        }
        
        # Katman 3: Bilinen ürünler (cache için)
        self.known_products_cache = {}
        
    def normalize(self, raw_text: str) -> Tuple[str, float]:
        """
        Ürün ismini normalize et
        
        Returns:
            (normalized_name, confidence): Normalize edilmiş isim ve güven skoru (0-1)
        """
        if not raw_text or len(raw_text) < 2:
            return ("Ürün", 0.0)
        
        logger.info(f"🔍 NORMALIZE BAŞLANGIÇ: '{raw_text}'")
        
        # ADIM 1: Ön temizlik
        text = raw_text.upper().strip()
        
        # ADIM 2: Sayıları ve birimleri kaldır
        text = self._remove_numbers_and_units(text)
        
        # ADIM 3: OCR hatalarını düzelt
        text = self._fix_ocr_errors(text)
        
        # ADIM 4: Pattern matching yap
        matched_product, confidence = self._match_product_pattern(text)
        
        if confidence > 0.7:
            logger.info(f"✅ PATTERN MATCH: '{raw_text}' → '{matched_product}' (güven: {confidence:.2f})")
            return (matched_product, confidence)
        
        # ADIM 5: Fallback - Basit temizlik
        cleaned = self._fallback_clean(text)
        
        logger.info(f"⚠️ FALLBACK: '{raw_text}' → '{cleaned}' (güven: 0.5)")
        return (cleaned, 0.5)
    
    def _remove_numbers_and_units(self, text: str) -> str:
        """Sayıları ve birimleri kaldır"""
        # Tüm sayıları kaldır
        text = re.sub(r'\d+[.,]?\d*', '', text)
        text = re.sub(r'[IO]\s*\d+', '', text)  # I 08, O8 gibi
        
        # Birimleri kaldır
        units = r'\b(KG|GR|GRAM|LT|LITRE|ML|ADET|ADT|AD|PCS|PC|G|L|X)\b'
        text = re.sub(units, '', text, flags=re.IGNORECASE)
        
        # Özel karakterler
        text = re.sub(r'[%#*«»\(\)\[\]{}.,;:!?\-_/\\|]', ' ', text)
        
        # Fazla boşluklar
        text = re.sub(r'\s+', ' ', text).strip()
        
        return text
    
    def _fix_ocr_errors(self, text: str) -> str:
        """OCR hatalarını düzelt - GÜÇLENDİRİLMİŞ"""
        # Yaygın OCR pattern'lerini düzelt (önce bunlar!)
        # "STYAHCAY" -> "SİYAH ÇAY"
        text = re.sub(r'STY', 'SİY', text)
        text = re.sub(r'SUT', 'SÜT', text)
        text = re.sub(r'CAY', 'ÇAY', text)
        text = re.sub(r'CANGA', '', text)  # Marka eki
        text = re.sub(r'FLZ', '', text)
        text = re.sub(r'DOGS', '', text)
        text = re.sub(r'KGDOGS', '', text)
        
        # Bilinen hataları düzelt
        for wrong, correct in self.ocr_fixes.items():
            text = text.replace(wrong, correct)
        
        return text
    
    def _match_product_pattern(self, text: str) -> Tuple[str, float]:
        """Pattern matching ile ürün tanı"""
        best_match = None
        best_confidence = 0.0
        
        for pattern, product_name in self.product_patterns.items():
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                # Eşleşme kalitesini hesapla
                match_ratio = len(match.group(0)) / len(text)
                confidence = min(0.95, match_ratio * 1.2)  # Max 0.95
                
                if confidence > best_confidence:
                    best_match = product_name
                    best_confidence = confidence
        
        if best_match:
            return (best_match, best_confidence)
        
        return ("", 0.0)
    
    def _fallback_clean(self, text: str) -> str:
        """Fallback temizlik - hiçbir pattern eşleşmezse"""
        # Çok kısa kelimeleri kaldır
        words = text.split()
        words = [w for w in words if len(w) > 2]
        
        if not words:
            return "Ürün"
        
        # Title case yap
        cleaned = ' '.join(words).title()
        
        # Türkçe karakter düzeltmeleri
        cleaned = cleaned.replace('I', 'ı').replace('İ', 'i')
        
        return cleaned if len(cleaned) >= 3 else "Ürün"
    
    def fuzzy_match_database(self, text: str, db_products: list) -> Optional[str]:
        """
        Veritabanındaki ürünlerle fuzzy matching yap
        
        Args:
            text: Normalize edilmiş metin
            db_products: [(product_id, product_name), ...] listesi
        
        Returns:
            En iyi eşleşen ürün adı veya None
        """
        if not db_products:
            return None
        
        best_match = None
        best_ratio = 0.0
        
        for prod_id, prod_name in db_products:
            ratio = SequenceMatcher(None, text.upper(), prod_name.upper()).ratio()
            
            if ratio > best_ratio and ratio > 0.75:  # %75+ benzerlik
                best_match = prod_name
                best_ratio = ratio
        
        if best_match:
            logger.info(f"🎯 FUZZY MATCH: '{text}' → '{best_match}' (benzerlik: {best_ratio:.2f})")
            return best_match
        
        return None


# Global instance
_normalizer = ProductNormalizerAdvanced()


def normalize_product_name(raw_text: str, db_products: list = None) -> Tuple[str, float]:
    """
    Ürün ismini normalize et (wrapper function)
    
    Args:
        raw_text: Ham OCR metni
        db_products: Opsiyonel veritabanı ürünleri listesi
    
    Returns:
        (normalized_name, confidence)
    """
    # Temel normalizasyon
    name, confidence = _normalizer.normalize(raw_text)
    
    # Eğer güven düşükse ve db varsa, fuzzy match dene
    if confidence < 0.8 and db_products:
        fuzzy_match = _normalizer.fuzzy_match_database(name, db_products)
        if fuzzy_match:
            return (fuzzy_match, 0.9)
    
    return (name, confidence)

