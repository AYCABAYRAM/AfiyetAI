# -*- coding: utf-8 -*-
"""
Pattern Matching Tabanlı Ürün Normalizasyon Sistemi
Mevcut etiketlenmiş verileri kullanarak yeni metinleri normalize eder
"""

import logging
import re
import unicodedata
from typing import Optional, Tuple, List, Dict
from dataclasses import dataclass
import os
from dotenv import load_dotenv
import psycopg2
from difflib import SequenceMatcher
from collections import defaultdict

logger = logging.getLogger(__name__)

@dataclass
class NormalizationResult:
    """Normalizasyon sonucu"""
    normalized_name: str
    confidence: float
    method: str
    original_text: str

class PatternProductNormalizer:
    """Pattern matching tabanlı ürün normalizasyon sistemi"""
    
    def __init__(self):
        load_dotenv()
        self.patterns = {}
        self.keywords = defaultdict(list)
        self._load_patterns_from_db()
        
        # Non-food keywords - bunlar filtrelenmeli
        self.non_food_keywords = [
            'peçete', 'peçet', 'tissue', 'paper', 'kağıt', 'kagit',
            'plastik', 'plastic', 'poşet', 'poset', 'bag',
            'kart', 'card', 'indirim', 'discount', 'promosyon',
            'efsane', 'kiyak', 'marka', 'brand', 'kod', 'code',
            'numara', 'number', 'adres', 'address', 'telefon', 'phone',
            'tarih', 'date', 'saat', 'time', 'toplam', 'total',
            'kdv', 'tax', 'vergi', 'tax', 'para', 'money',
            'kredi', 'credit', 'banka', 'bank', 'atm', 'pos',
            'remy', 'alkol', 'alcohol', 'sigara', 'cigarette',
        ]
        
        # Sayı ve birim temizleme
        self.quantity_patterns = [
            r'\d+\s*(kg|g|gr|gram|kilogram|litre|lt|l|ml|adet|pcs|piece|paket|pack)',
            r'\d+\s*x\s*\d+',  # 2x3 gibi
            r'\d+\s*%\s*\d+',  # %20 gibi
            r'\d+\s*,\s*\d+',  # 1,5 gibi
            r'\d+\.\d+',       # 1.5 gibi
            r'\d+',            # Sadece sayılar
        ]
        
        # Fiyat ve para birimi temizleme
        self.price_patterns = [
            r'\d+\s*(tl|lira|₺|€|$|usd|eur)',
            r'\d+\s*,\s*\d+\s*(tl|lira|₺|€|$|usd|eur)',
            r'\d+\.\d+\s*(tl|lira|₺|€|$|usd|eur)',
            r'\d+\s*(tl|lira|₺|€|$|usd|eur)\s*\d+',
        ]
    
    def _load_patterns_from_db(self):
        """Veritabanından pattern'leri yükle"""
        try:
            conn = psycopg2.connect(os.environ['DATABASE_URL'])
            cursor = conn.cursor()
            
            # Etiketlenmiş verileri yükle
            cursor.execute('''
                SELECT raw_text, normalized_text_tr 
                FROM receipt_normalizations 
                WHERE normalized_text_tr IS NOT NULL 
                AND normalized_text_tr != 'null'
                AND normalized_text_tr != raw_text
            ''')
            results = cursor.fetchall()
            
            # Pattern'leri oluştur
            for raw_text, normalized_text in results:
                if not raw_text or not normalized_text:
                    continue
                    
                # Temizlenmiş metinler
                clean_raw = self._basic_cleanup(raw_text)
                clean_norm = normalized_text.strip()
                
                if len(clean_raw) < 3 or len(clean_norm) < 3:
                    continue
                
                # Anahtar kelimeleri çıkar
                raw_words = set(clean_raw.split())
                norm_words = set(clean_norm.lower().split())
                
                # Her kelime için pattern oluştur
                for word in raw_words:
                    if len(word) > 2:  # Çok kısa kelimeleri atla
                        self.keywords[word].append(clean_norm)
                
                # Tam metin pattern'i
                self.patterns[clean_raw] = clean_norm
            
            cursor.close()
            conn.close()
            
            logger.info(f"✅ {len(self.patterns)} pattern ve {len(self.keywords)} anahtar kelime yüklendi")
            
        except Exception as e:
            logger.error(f"❌ Pattern yükleme hatası: {e}")
            self.patterns = {}
            self.keywords = defaultdict(list)
    
    def normalize(self, raw_text: str) -> Optional[NormalizationResult]:
        """
        Ham OCR metnini normalize et
        """
        if not raw_text or len(raw_text.strip()) < 3:
            return None
        
        original_text = raw_text.strip()
        
        # 1. Non-food kontrolü
        if self._is_non_food(original_text):
            logger.info(f"🚫 Non-food item filtered: '{original_text}'")
            return None
        
        # 2. Temel temizlik
        cleaned_text = self._basic_cleanup(original_text)
        
        # 3. Sayı ve birimleri temizle
        quantity_cleaned_text = self._remove_quantities(cleaned_text)
        
        # 4. Fiyat bilgilerini temizle
        price_cleaned_text = self._remove_prices(quantity_cleaned_text)
        
        # 5. Tam eşleşme kontrolü
        if price_cleaned_text in self.patterns:
            return NormalizationResult(
                normalized_name=self.patterns[price_cleaned_text],
                confidence=0.95,
                method="exact_match",
                original_text=original_text
            )
        
        # 6. Anahtar kelime tabanlı eşleşme
        best_match = self._find_keyword_match(price_cleaned_text)
        
        if best_match and best_match['confidence'] > 0.6:
            return NormalizationResult(
                normalized_name=best_match['name'],
                confidence=best_match['confidence'],
                method="keyword_match",
                original_text=original_text
            )
        
        # 7. Fuzzy matching
        fuzzy_match = self._find_fuzzy_match(price_cleaned_text)
        
        if fuzzy_match and fuzzy_match['similarity'] > 0.7:
            return NormalizationResult(
                normalized_name=fuzzy_match['name'],
                confidence=fuzzy_match['similarity'],
                method="fuzzy_match",
                original_text=original_text
            )
        
        # 8. Son çare: temizlenmiş metni döndür
        final_text = self._final_cleanup(price_cleaned_text)
        
        if len(final_text) < 3:
            return None
        
        return NormalizationResult(
            normalized_name=final_text.title(),
            confidence=0.3,
            method="basic_cleanup",
            original_text=original_text
        )
    
    def _is_non_food(self, text: str) -> bool:
        """Non-food item kontrolü"""
        text_lower = text.lower()
        
        # Kesin non-food kelimeler
        strict_non_food = ['peçete', 'peçet', 'tissue', 'paper', 'kağıt', 'kagit', 'plastik', 'plastic', 'poşet', 'poset', 'bag', 'kart', 'card', 'indirim', 'discount', 'promosyon', 'remy']
        
        for keyword in strict_non_food:
            if keyword in text_lower:
                return True
                
        return False
    
    def _basic_cleanup(self, text: str) -> str:
        """Temel metin temizliği"""
        # Unicode normalizasyonu
        text = unicodedata.normalize('NFKD', text)
        
        # Büyük/küçük harf düzeltme
        text = text.lower()
        
        # Fazla boşlukları temizle
        text = re.sub(r'\s+', ' ', text)
        
        # Başta ve sonda boşlukları temizle
        text = text.strip()
        
        return text
    
    def _remove_quantities(self, text: str) -> str:
        """Sayı ve birimleri kaldır"""
        for pattern in self.quantity_patterns:
            text = re.sub(pattern, '', text, flags=re.IGNORECASE)
        return text
    
    def _remove_prices(self, text: str) -> str:
        """Fiyat bilgilerini kaldır"""
        for pattern in self.price_patterns:
            text = re.sub(pattern, '', text, flags=re.IGNORECASE)
        return text
    
    def _find_keyword_match(self, text: str) -> Optional[Dict]:
        """Anahtar kelime tabanlı eşleşme"""
        text_words = text.split()
        matches = defaultdict(int)
        
        for word in text_words:
            if word in self.keywords:
                for normalized in self.keywords[word]:
                    matches[normalized] += 1
        
        if not matches:
            return None
        
        # En çok eşleşen ürünü bul
        best_match = max(matches.items(), key=lambda x: x[1])
        
        # Confidence hesapla (eşleşen kelime sayısı / toplam kelime sayısı)
        confidence = best_match[1] / len(text_words)
        
        return {
            'name': best_match[0],
            'confidence': confidence,
            'matches': best_match[1]
        }
    
    def _find_fuzzy_match(self, text: str) -> Optional[Dict]:
        """Fuzzy matching ile en yakın pattern'i bul"""
        best_match = None
        best_similarity = 0
        
        for pattern, normalized in self.patterns.items():
            similarity = SequenceMatcher(None, text, pattern).ratio()
            
            if similarity > best_similarity:
                best_similarity = similarity
                best_match = {
                    'name': normalized,
                    'similarity': similarity,
                    'pattern': pattern
                }
        
        return best_match
    
    def _final_cleanup(self, text: str) -> str:
        """Son temizlik"""
        # Fazla boşlukları temizle
        text = re.sub(r'\s+', ' ', text)
        
        # Başta ve sonda boşlukları temizle
        text = text.strip()
        
        # Noktalama işaretlerini temizle
        text = re.sub(r'[^\w\s]', '', text)
        
        return text

# Test fonksiyonu
def test_pattern_normalizer():
    """Pattern normalizasyon sistemini test et"""
    normalizer = PatternProductNormalizer()
    
    test_cases = [
        "BURCU KONS KOZ PATLI 40 35",
        "NAMET 24 PILIC FUM 50",
        "ULKER KARE SUTL XI 60 72 95",
        "KIVIRCIK ADET YI 45290",
        "M4 HELNZ KETCAP 375GRM Xi 70 36 15",
        "MAKARNA SPAGETTI",
        "BANVIT PILIC BONFİLE x1 x 266 , 46 MIGROS PLASTIK POSET %20 x0,50",
        "MEYVE NEK ŞETLİ 1LT",
        "YBN MER",
        "UN EFSANE",
        "PEÇETE",
        "REMY",
        "FESLEĞEN",
        "YUMURTA",
        "PIRINÇ",
    ]
    
    print("🧪 Pattern Normalizasyon Test Sonuçları:")
    print("=" * 60)
    
    for test_case in test_cases:
        result = normalizer.normalize(test_case)
        if result:
            print(f"✅ '{test_case}' → '{result.normalized_name}' ({result.method}, {result.confidence:.2f})")
        else:
            print(f"❌ '{test_case}' → FILTERED")

if __name__ == "__main__":
    test_pattern_normalizer()
