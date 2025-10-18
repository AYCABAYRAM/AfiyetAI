# recipe_recommender.py
# -*- coding: utf-8 -*-
"""
Raf ömrüne göre önceliklendirilmiş tarif önerisi sistemi
"""
import logging
import requests
import os
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Tuple
import translate_utils
from dataclasses import dataclass

from sqlalchemy import text
from dotenv import load_dotenv

from db import get_engine
from translate_utils import translate_text

# .env dosyasını yükle
load_dotenv()

logger = logging.getLogger(__name__)

@dataclass
class ProductWithShelfLife:
    """Raf ömrü bilgisi olan ürün"""
    product_id: int
    name_en: str
    name_tr: str
    days_remaining: int
    storage_type: str
    open_state: str
    priority_score: float

@dataclass
class RecipeRecommendation:
    """Tarif önerisi"""
    recipe_id: int
    title: str
    title_tr: str
    image: str
    ready_in_minutes: int
    servings: int
    source_url: str
    used_products: List[str]
    used_products_tr: List[str]
    missing_products: List[str]
    missing_products_tr: List[str]
    priority_score: float
    shelf_life_urgency: str
    instructions: str
    summary: str

class RecipeRecommender:
    """Raf ömrüne göre tarif önerisi yapan sınıf"""
    
    def __init__(self):
        self.spoonacular_api_key = os.getenv("SPOONACULAR_API_KEY")
        self.base_url = "https://api.spoonacular.com/recipes"
        self.engine = get_engine()
        
        if not self.spoonacular_api_key:
            raise ValueError("SPOONACULAR_API_KEY bulunamadı!")
    
    def get_user_preferences(self, user_id: int = 1) -> Dict:
        """Kullanıcının kişiselleştirme tercihlerini getir"""
        with self.engine.connect() as conn:
            preferences = {
                'allergies': [],
                'dislikes': [],
                'dietary_preferences': [],
                'liked_categories': []
            }
            
            # Alerjileri getir
            result = conn.execute(text("""
                SELECT a.name 
                FROM user_allergies ua
                JOIN allergens a ON ua.allergen_id = a.allergen_id
                WHERE ua.user_id = :user_id
            """), {"user_id": user_id})
            preferences['allergies'] = [row[0] for row in result]
            
            # Sevilmeyen ürünleri getir
            result = conn.execute(text("""
                SELECT p.canonical_name_en 
                FROM user_dislikes ud
                JOIN products p ON ud.product_id = p.product_id
                WHERE ud.user_id = :user_id
            """), {"user_id": user_id})
            preferences['dislikes'] = [row[0] for row in result]
            
            # Diyet tercihlerini getir
            result = conn.execute(text("""
                SELECT dp.code, dp.label 
                FROM user_dietary_preferences udp
                JOIN dietary_preferences dp ON udp.pref_id = dp.pref_id
                WHERE udp.user_id = :user_id
            """), {"user_id": user_id})
            preferences['dietary_preferences'] = [{'code': row[0], 'label': row[1]} for row in result]
            
            # Geçmiş tarif tercihlerinden kategori analizi
            result = conn.execute(text("""
                SELECT COUNT(*) as count, 'unknown' as category
                FROM recipe_recommendations 
                WHERE user_id = :user_id AND clicked_at IS NOT NULL
                GROUP BY category
                ORDER BY count DESC
                LIMIT 5
            """), {"user_id": user_id})
            preferences['liked_categories'] = [row[1] for row in result]
            
            logger.info(f"🔍 Kullanıcı {user_id} tercihleri: {preferences}")
            return preferences
    
    def get_user_inventory(self, user_id: int = 1) -> List[ProductWithShelfLife]:
        """Kullanıcının envanterini raf ömrü bilgisiyle getir"""
        with self.engine.connect() as conn:
            # Kullanıcının envanterini ve raf ömrü bilgilerini al
            result = conn.execute(text("""
                SELECT 
                    ib.product_id,
                    p.canonical_name_en,
                    COALESCE(pt.translated_text, p.canonical_name_en) as name_tr,
                    ib.qty,
                    ib.expected_expiry_date,
                    slr.days as shelf_life_days,
                    s.name as storage_name,
                    slr.open_state,
                    ib.created_at
                FROM inventory_batches ib
                JOIN products p ON ib.product_id = p.product_id
                LEFT JOIN product_translations pt ON p.product_id = pt.product_id 
                    AND pt.source_lang = 'en' AND pt.target_lang = 'tr'
                LEFT JOIN shelf_life_rules slr ON p.product_id = slr.product_id
                LEFT JOIN storage s ON slr.storage_id = s.storage_id
                WHERE ib.user_id = :user_id
                AND ib.qty > 0
                AND ib.status = 'in_stock'
                AND (ib.expected_expiry_date IS NULL OR ib.expected_expiry_date > CURRENT_DATE)
                ORDER BY 
                    CASE 
                        WHEN ib.expected_expiry_date IS NOT NULL THEN ib.expected_expiry_date
                        ELSE CURRENT_DATE + INTERVAL '30 days'
                    END ASC
            """), {"user_id": user_id})
            
            products = []
            for row in result:
                # Raf ömrü hesapla
                if row.expected_expiry_date:
                    days_remaining = (row.expected_expiry_date - datetime.now().date()).days
                elif row.shelf_life_days:
                    # Tahmini raf ömrü
                    days_remaining = row.shelf_life_days
                else:
                    days_remaining = 30  # Varsayılan
                
                # Öncelik skoru hesapla (düşük gün = yüksek öncelik)
                if days_remaining <= 0:
                    priority_score = 100.0  # Çok acil
                elif days_remaining <= 3:
                    priority_score = 80.0   # Acil
                elif days_remaining <= 7:
                    priority_score = 60.0   # Orta
                elif days_remaining <= 14:
                    priority_score = 40.0   # Düşük
                else:
                    priority_score = 20.0   # Çok düşük
                
                products.append(ProductWithShelfLife(
                    product_id=row.product_id,
                    name_en=row.canonical_name_en,
                    name_tr=row.name_tr,
                    days_remaining=days_remaining,
                    storage_type=row.storage_name or "unknown",
                    open_state=row.open_state or "sealed",
                    priority_score=priority_score
                ))
            
            return products
    
    def search_recipes_by_ingredients(self, ingredients: List[str], number: int = 10, user_id: int = 1) -> List[Dict]:
        """Malzemelerle tarif ara - kişiselleştirme ile"""
        try:
            logger.info(f"🔍 Spoonacular API çağrısı: {ingredients[:5]}")
            logger.info(f"🔑 API Key: {self.spoonacular_api_key[:10]}...")
            
            # Kullanıcı tercihlerini al
            preferences = self.get_user_preferences(user_id)
            
            # Spoonacular API'ye istek gönder
            url = f"{self.base_url}/findByIngredients"
            params = {
                'ingredients': ','.join(ingredients[:10]),  # Max 10 malzeme
                'number': number * 2,  # Daha fazla tarif al, sonra filtrele
                'apiKey': self.spoonacular_api_key,
                'ranking': 2,  # Minimize missing ingredients
                'ignorePantry': False
            }
            
            # Diyet tercihlerini ekle
            diet_codes = [pref['code'] for pref in preferences['dietary_preferences']]
            if diet_codes:
                params['diet'] = ','.join(diet_codes)
                logger.info(f"🥗 Diyet tercihleri: {diet_codes}")
            
            logger.info(f"🌐 URL: {url}")
            logger.info(f"📋 Params: {params}")
            
            response = requests.get(url, params=params, timeout=30)
            logger.info(f"📡 Response Status: {response.status_code}")
            
            if response.status_code != 200:
                logger.error(f"❌ API Error {response.status_code}: {response.text[:200]}")
                return []
            
            recipes = response.json()
            logger.info(f"✅ API Success: {len(recipes)} recipes found")
            
            # Kişiselleştirme filtreleri uygula
            filtered_recipes = self._apply_personalization_filters(recipes, preferences, ingredients)
            
            # Her tarif için detaylı bilgileri al
            detailed_recipes = []
            for recipe in filtered_recipes[:5]:  # İlk 5 tarif için detay al
                recipe_id = recipe.get('id')
                if recipe_id:
                    details = self.get_recipe_details(recipe_id)
                    if details:
                        # sourceUrl'i kontrol et ve düzelt
                        source_url = details.get('sourceUrl', recipe.get('sourceUrl', ''))
                        if not source_url or source_url == '':
                            # Spoonacular'ın kendi tarif sayfasını kullan
                            source_url = f"https://spoonacular.com/recipes/{recipe_id}"
                        
                        # Temel bilgileri detaylarla birleştir
                        enhanced_recipe = {
                            **recipe,  # Orijinal bilgiler
                            'readyInMinutes': details.get('readyInMinutes', recipe.get('readyInMinutes', 0)),
                            'servings': details.get('servings', recipe.get('servings', 0)),
                            'sourceUrl': source_url,
                            'instructions': details.get('instructions', ''),
                            'summary': details.get('summary', ''),
                            'cuisines': details.get('cuisines', []),
                            'dishTypes': details.get('dishTypes', [])
                        }
                        detailed_recipes.append(enhanced_recipe)
                    else:
                        detailed_recipes.append(recipe)
                else:
                    detailed_recipes.append(recipe)
            
            logger.info(f"🔍 Detaylı bilgiler alındı: {len(detailed_recipes)} tarif")
            return detailed_recipes
            
        except Exception as e:
            logger.error(f"❌ Tarif arama hatası: {e}")
            return []
    
    def _apply_personalization_filters(self, recipes: List[Dict], preferences: Dict, ingredients: List[str]) -> List[Dict]:
        """Kişiselleştirme filtrelerini uygula"""
        filtered_recipes = []
        
        for recipe in recipes:
            # 1. Alerji kontrolü
            if self._has_allergens(recipe, preferences['allergies']):
                logger.info(f"🚫 Tarif alerji nedeniyle filtrelendi: {recipe.get('title', 'Unknown')}")
                continue
            
            # 2. Dislike kontrolü
            if self._has_disliked_ingredients(recipe, preferences['dislikes']):
                logger.info(f"👎 Tarif dislike nedeniyle filtrelendi: {recipe.get('title', 'Unknown')}")
                continue
            
            # 3. Eksik malzeme kontrolü (çok fazla eksik malzeme varsa filtrele)
            missing_count = len(recipe.get('missedIngredients', []))
            if missing_count > 8:  # Maksimum 8 eksik malzeme (daha esnek)
                logger.info(f"❌ Çok fazla eksik malzeme ({missing_count}): {recipe.get('title', 'Unknown')}")
                continue
            
            # 4. Kişiselleştirme skoru ekle
            recipe['personalization_score'] = self._calculate_personalization_score(recipe, preferences, ingredients)
            
            filtered_recipes.append(recipe)
        
        # Kişiselleştirme skoruna göre sırala
        filtered_recipes.sort(key=lambda x: x.get('personalization_score', 0), reverse=True)
        
        logger.info(f"🔍 {len(recipes)} tariften {len(filtered_recipes)} tanesi kişiselleştirme filtresinden geçti")
        return filtered_recipes
    
    def _has_allergens(self, recipe: Dict, allergies: List[str]) -> bool:
        """Tarifte alerjen var mı kontrol et"""
        if not allergies:
            return False
        
        # Kullanılan malzemeleri kontrol et
        used_ingredients = [ing.get('name', '').lower() for ing in recipe.get('usedIngredients', [])]
        missed_ingredients = [ing.get('name', '').lower() for ing in recipe.get('missedIngredients', [])]
        all_ingredients = used_ingredients + missed_ingredients
        
        for allergy in allergies:
            allergy_lower = allergy.lower()
            for ingredient in all_ingredients:
                if allergy_lower in ingredient or ingredient in allergy_lower:
                    return True
        
        return False
    
    def _has_disliked_ingredients(self, recipe: Dict, dislikes: List[str]) -> bool:
        """Tarifte sevilmeyen malzeme var mı kontrol et"""
        if not dislikes:
            return False
        
        # Kullanılan malzemeleri kontrol et
        used_ingredients = [ing.get('name', '').lower() for ing in recipe.get('usedIngredients', [])]
        
        for dislike in dislikes:
            dislike_lower = dislike.lower()
            for ingredient in used_ingredients:
                if dislike_lower in ingredient or ingredient in dislike_lower:
                    return True
        
        return False
    
    def _calculate_personalization_score(self, recipe: Dict, preferences: Dict, ingredients: List[str]) -> float:
        """Kişiselleştirme skoru hesapla"""
        score = 0.0
        
        # 1. Temel skor (eksik malzeme sayısına göre)
        missing_count = len(recipe.get('missedIngredients', []))
        used_count = len(recipe.get('usedIngredients', []))
        total_ingredients = missing_count + used_count
        
        if total_ingredients > 0:
            score += (used_count / total_ingredients) * 50  # %50'ye kadar temel skor
        
        # 2. Diyet tercihleri bonusu
        if preferences['dietary_preferences']:
            # Spoonacular API zaten diyet filtrelemesi yapıyor, bonus ver
            score += 20
        
        # 3. Kategori tercihleri bonusu (şimdilik basit)
        if preferences['liked_categories']:
            score += 10
        
        # 4. Fiş malzemeleriyle eşleşme bonusu
        used_ingredient_names = [ing.get('name', '').lower() for ing in recipe.get('usedIngredients', [])]
        ingredient_matches = 0
        for ingredient in ingredients:
            ingredient_lower = ingredient.lower()
            for used_ing in used_ingredient_names:
                if ingredient_lower in used_ing or used_ing in ingredient_lower:
                    ingredient_matches += 1
                    break
        
        if len(ingredients) > 0:
            match_ratio = ingredient_matches / len(ingredients)
            score += match_ratio * 20  # %20'ye kadar eşleşme bonusu
        
        return min(score, 100.0)  # Maksimum 100 puan
    
    def get_recipe_details(self, recipe_id: int) -> Optional[Dict]:
        """Tarif detaylarını al"""
        try:
            # Ana bilgileri al
            url = f"{self.base_url}/{recipe_id}/information"
            params = {
                'apiKey': self.spoonacular_api_key,
                'includeNutrition': False
            }
            
            response = requests.get(url, params=params, timeout=30)
            response.raise_for_status()
            recipe_data = response.json()
            
            # Instructions'ı ayrı endpoint'ten al
            try:
                instructions_url = f"{self.base_url}/{recipe_id}/analyzedInstructions"
                instructions_params = {
                    'apiKey': self.spoonacular_api_key,
                    'stepBreakdown': True
                }
                
                instructions_response = requests.get(instructions_url, params=instructions_params, timeout=30)
                if instructions_response.status_code == 200:
                    instructions_data = instructions_response.json()
                    if instructions_data and len(instructions_data) > 0:
                        # Instructions'ı HTML formatından temizle ve birleştir
                        steps = []
                        for step_group in instructions_data:
                            for step in step_group.get('steps', []):
                                step_text = step.get('step', '')
                                if step_text:
                                    # HTML taglarını temizle
                                    import re
                                    clean_text = re.sub(r'<[^>]+>', '', step_text)
                                    steps.append(clean_text)
                        
                        if steps:
                            recipe_data['instructions'] = ' '.join(steps)
                        else:
                            recipe_data['instructions'] = ''
                    else:
                        recipe_data['instructions'] = ''
                else:
                    recipe_data['instructions'] = ''
                    
            except Exception as e:
                logger.warning(f"Instructions alma hatası {recipe_id}: {e}")
                recipe_data['instructions'] = ''
            
            return recipe_data
            
        except Exception as e:
            logger.error(f"Tarif detay hatası {recipe_id}: {e}")
            return None

    def _get_detailed_recipes(self, recipes: List[Dict]) -> List[Dict]:
        """findByIngredients sonuçları için detayları getirip birleştirir."""
        detailed_recipes: List[Dict] = []
        try:
            for recipe in recipes[:5]:  # İlk 5 tarif için detay al
                recipe_id = recipe.get('id')
                if not recipe_id:
                    detailed_recipes.append(recipe)
                    continue
                details = self.get_recipe_details(recipe_id)
                if not details:
                    detailed_recipes.append(recipe)
                    continue
                # sourceUrl'i kontrol et ve düzelt
                source_url = details.get('sourceUrl', recipe.get('sourceUrl', ''))
                if not source_url or source_url == '':
                    # Spoonacular'ın kendi tarif sayfasını kullan
                    source_url = f"https://spoonacular.com/recipes/{recipe_id}"
                
                enhanced_recipe = {
                    **recipe,
                    'readyInMinutes': details.get('readyInMinutes', recipe.get('readyInMinutes', 0)),
                    'servings': details.get('servings', recipe.get('servings', 0)),
                    'sourceUrl': source_url,
                    'instructions': details.get('instructions', ''),
                    'summary': details.get('summary', ''),
                    'cuisines': details.get('cuisines', []),
                    'dishTypes': details.get('dishTypes', [])
                }
                detailed_recipes.append(enhanced_recipe)
        except Exception as e:
            logger.error(f"Detaylı tarif getirme hatası: {e}")
            return recipes
        logger.info(f"🔍 Detaylı bilgiler alındı: {len(detailed_recipes)} tarif")
        return detailed_recipes
    
    def calculate_recipe_priority(self, recipe: Dict, user_products: List[ProductWithShelfLife]) -> Tuple[float, str]:
        """Tarif önceliğini hesapla - geliştirilmiş eşleştirme"""
        used_ingredients = recipe.get('usedIngredients', [])
        missed_ingredients = recipe.get('missedIngredients', [])
        
        # Kullanılan malzemelerin öncelik skorlarını topla
        total_priority = 0
        used_count = 0
        matched_ingredients = []
        
        # Geliştirilmiş eşleştirme
        for ingredient in used_ingredients:
            ingredient_name = ingredient.get('name', '').lower()
            matched = False
            
            for product in user_products:
                # Daha esnek eşleştirme
                if self._ingredient_matches_product(ingredient_name, product):
                    total_priority += product.priority_score
                    used_count += 1
                    matched_ingredients.append(ingredient_name)
                    matched = True
                    break
            
            # Eşleşme bulunamadıysa, benzer ürün ara
            if not matched:
                for product in user_products:
                    if self._find_similar_ingredient(ingredient_name, product):
                        total_priority += product.priority_score * 0.7  # %70 skor
                        used_count += 1
                        matched_ingredients.append(f"{ingredient_name} (~{product.name_tr})")
                        matched = True
                        break
        
        if used_count == 0:
            return 0.0, "no_match"
        
        # Ortalama öncelik skoru
        avg_priority = total_priority / used_count
        
        # Eksik malzeme cezası - daha az agresif
        missing_penalty = len(missed_ingredients) * 2  # 5'ten 2'ye düşürüldü
        
        # Kullanım oranı bonusu
        usage_ratio = used_count / (used_count + len(missed_ingredients))
        usage_bonus = usage_ratio * 20  # %20'ye kadar bonus
        
        # Final skor
        final_score = max(0, avg_priority - missing_penalty + usage_bonus)
        
        # Aciliyet seviyesi
        if final_score >= 80:
            urgency = "çok_acil"
        elif final_score >= 60:
            urgency = "acil"
        elif final_score >= 40:
            urgency = "orta"
        elif final_score >= 20:
            urgency = "düşük"
        else:
            urgency = "çok_düşük"
        
        return final_score, urgency
    
    def _ingredient_matches_product(self, ingredient_name: str, product: ProductWithShelfLife) -> bool:
        """Malzeme-ürün eşleştirmesi"""
        # Temel eşleştirme
        if (ingredient_name in product.name_en.lower() or 
            ingredient_name in product.name_tr.lower()):
            return True
        
        # Geliştirilmiş eşleştirme kuralları
        matches = {
            # Yumurta
            'eggs': ['yumurta', 'egg'],
            'egg': ['yumurta', 'eggs'],
            
            # Süt ürünleri
            'milk': ['süt', 'milk'],
            'cheese': ['peynir', 'cheese'],
            'cream cheese': ['krem peynir', 'cream cheese'],
            'mozzarella': ['mozzarella', 'peynir'],
            
            # Sebzeler
            'tomatoes': ['domates', 'tomato'],
            'tomato': ['domates', 'tomatoes'],
            'onions': ['soğan', 'onion'],
            'onion': ['soğan', 'onions'],
            'potatoes': ['patates', 'potato'],
            'potato': ['patates', 'potatoes'],
            
            # Tahıllar
            'bread': ['ekmek', 'bread'],
            'rice': ['pirinç', 'rice'],
            'pasta': ['makarna', 'pasta'],
            
            # Et
            'chicken': ['tavuk', 'chicken'],
            'beef': ['biftek', 'beef', 'dana'],
            'meat': ['et', 'meat'],
            
            # Diğer
            'butter': ['tereyağı', 'butter'],
            'oil': ['yağ', 'oil'],
            'salt': ['tuz', 'salt'],
            'pepper': ['biber', 'pepper'],
        }
        
        for key, values in matches.items():
            if key in ingredient_name:
                for value in values:
                    if value in product.name_en.lower() or value in product.name_tr.lower():
                        return True
        
        return False
    
    def _find_similar_ingredient(self, ingredient_name: str, product: ProductWithShelfLife) -> bool:
        """Benzer malzeme bulma"""
        # Genel kategoriler
        categories = {
            'dairy': ['süt', 'peynir', 'yoğurt', 'milk', 'cheese', 'yogurt'],
            'vegetables': ['domates', 'soğan', 'patates', 'havuç', 'tomato', 'onion', 'potato', 'carrot'],
            'grains': ['ekmek', 'pirinç', 'makarna', 'un', 'bread', 'rice', 'pasta', 'flour'],
            'protein': ['tavuk', 'et', 'biftek', 'yumurta', 'chicken', 'meat', 'beef', 'egg'],
            'spices': ['tuz', 'biber', 'baharat', 'salt', 'pepper', 'spice'],
        }
        
        for category, items in categories.items():
            if any(item in ingredient_name for item in items):
                if any(item in product.name_en.lower() or item in product.name_tr.lower() for item in items):
                    return True
        
        return False
    
    def _filter_essential_missing(self, missing_products: List[str]) -> List[str]:
        """Eksik malzemeleri filtrele - sadece gerçekten gerekli olanları göster"""
        # Çok temel malzemeler - genelde her evde bulunur
        basic_ingredients = {
            'salt', 'pepper', 'oil', 'butter', 'flour', 'sugar', 'garlic', 'onion',
            'tuz', 'biber', 'yağ', 'tereyağı', 'un', 'şeker', 'sarımsak', 'soğan',
            'water', 'su', 'vinegar', 'sirke', 'lemon', 'limon', 'herbs', 'otlar'
        }
        
        # Baharatlar ve soslar - opsiyonel
        optional_ingredients = {
            'sauce', 'sos', 'spice', 'baharat', 'seasoning', 'baharat', 'herb', 'ot',
            'condiment', 'sos', 'dressing', 'sos', 'marinade', 'marine'
        }
        
        # Filtrelenmiş liste
        essential_missing = []
        for ingredient in missing_products:
            ingredient_lower = ingredient.lower()
            
            # Temel malzemeleri atla
            if any(basic in ingredient_lower for basic in basic_ingredients):
                continue
            
            # Opsiyonel malzemeleri atla
            if any(optional in ingredient_lower for optional in optional_ingredients):
                continue
            
            # Gerçekten gerekli malzemeleri ekle
            essential_missing.append(ingredient)
        
        return essential_missing[:3]  # Max 3 eksik malzeme göster
    
    def recommend_recipes_from_receipt(self, ingredients: List[str], max_recipes: int = 10, user_id: int = 1) -> List[RecipeRecommendation]:
        """Fiş ürünlerinden tarif öner - kişiselleştirme ile"""
        logger.info(f"🍽️ Fiş ürünlerinden tarif önerisi: {ingredients[:5]}")
        
        # Tarifleri ara (kişiselleştirme ile)
        recipes = self.search_recipes_by_ingredients(ingredients, max_recipes * 2, user_id)
        
        if not recipes:
            logger.warning("❌ Tarif bulunamadı!")
            return []
        
        logger.info(f"📋 {len(recipes)} tarif bulundu, önceliklendiriliyor...")
        
        # Detaylı bilgileri al
        detailed_recipes = self._get_detailed_recipes(recipes)
        
        # Tarif başlıklarını çevir
        for recipe in detailed_recipes:
            if 'title' in recipe:
                recipe['title_tr'] = translate_text(recipe['title'], source_lang='en', target_lang='tr')

        # Fiş ürünlerini ProductWithShelfLife formatına dönüştür
        receipt_products = []
        for i, ingredient in enumerate(ingredients):
            # Basit öncelik skoru - ilk ürünler daha önemli
            priority_score = max(20, 100 - (i * 10))
            
            receipt_products.append(ProductWithShelfLife(
                product_id=i + 1000,  # Geçici ID
                name_en=ingredient,
                name_tr=ingredient,  # Zaten çevrilmiş
                days_remaining=7,  # Varsayılan raf ömrü
                storage_type="unknown",
                open_state="sealed",
                priority_score=priority_score
            ))
        
        # Tarifleri önceliklendir
        recommendations = []
        for recipe in detailed_recipes:
            priority_score, urgency = self.calculate_recipe_priority(recipe, receipt_products)
            
            if priority_score >= 0:  # Tüm tarifleri al (daha esnek)
                used_products = [ing.get('name', '') for ing in recipe.get('usedIngredients', [])]
                missing_products = [ing.get('name', '') for ing in recipe.get('missedIngredients', [])]
                
                # Eksik malzemeleri filtrele
                essential_missing = self._filter_essential_missing(missing_products)
                
                # Türkçe çevirileri yap
                try:
                    title_tr = translate_utils.translate_text(recipe.get('title', ''))
                    used_products_tr = [translate_utils.translate_text(product) for product in used_products[:3]]
                    missing_products_tr = [translate_utils.translate_text(product) for product in essential_missing]
                except Exception as e:
                    logger.warning(f"Çeviri hatası: {e}")
                    title_tr = recipe.get('title', '')
                    used_products_tr = used_products[:3]
                    missing_products_tr = essential_missing
                
                recommendations.append(RecipeRecommendation(
                    recipe_id=recipe.get('id', 0),
                    title=recipe.get('title', ''),
                    title_tr=title_tr,
                    image=recipe.get('image', ''),
                    ready_in_minutes=recipe.get('readyInMinutes', 0),
                    servings=recipe.get('servings', 0),
                    source_url=recipe.get('sourceUrl', '') or f"https://spoonacular.com/recipes/{recipe.get('id', '')}" or "https://spoonacular.com/",
                    used_products=used_products,
                    used_products_tr=used_products_tr,
                    missing_products=essential_missing,
                    missing_products_tr=missing_products_tr,
                    priority_score=priority_score,
                    shelf_life_urgency=urgency,
                    instructions=recipe.get('instructions', ''),
                    summary=recipe.get('summary', '')
                ))
        
        # Öncelik skoruna göre sırala
        recommendations.sort(key=lambda x: x.priority_score, reverse=True)
        
        logger.info(f"✅ {len(recommendations)} öncelikli tarif hazırlandı")
        return recommendations[:max_recipes]
        """Kullanıcı için tarif öner"""
        print(f"🍽️  Kullanıcı {user_id} için tarif önerisi hazırlanıyor...")
        
        # Kullanıcının envanterini al
        user_products = self.get_user_inventory(user_id)
        
        if not user_products:
            print("❌ Kullanıcının envanterinde ürün bulunamadı!")
            return []
        
        print(f"📦 {len(user_products)} ürün bulundu")
        
        # En yüksek öncelikli ürünleri göster
        sorted_products = sorted(user_products, key=lambda x: x.priority_score, reverse=True)
        print(f"\n🔝 EN YÜKSEK ÖNCELİKLİ ÜRÜNLER:")
        for i, product in enumerate(sorted_products[:5], 1):
            urgency = "🔴 ÇOK ACİL" if product.priority_score >= 80 else \
                     "🟠 ACİL" if product.priority_score >= 60 else \
                     "🟡 ORTA" if product.priority_score >= 40 else "🟢 DÜŞÜK"
            print(f"  {i}. {product.name_tr} - {product.days_remaining} gün kaldı {urgency}")
        
        # Tarif arama için malzeme listesi hazırla
        ingredients = [p.name_en for p in sorted_products[:15]]  # En öncelikli 15 ürün
        
        print(f"\n🔍 Tarif aranıyor: {', '.join(ingredients[:5])}...")
        
        # Tarifleri ara
        recipes = self.search_recipes_by_ingredients(ingredients, max_recipes * 2)
        
        if not recipes:
            print("❌ Tarif bulunamadı!")
            return []
        
        print(f"📋 {len(recipes)} tarif bulundu, önceliklendiriliyor...")
        
        # Tarifleri önceliklendir
        recommendations = []
        for recipe in recipes:
            priority_score, urgency = self.calculate_recipe_priority(recipe, user_products)
            
            if priority_score > 0:  # Sadece eşleşen tarifleri al
                used_products = [ing.get('name', '') for ing in recipe.get('usedIngredients', [])]
                missing_products = [ing.get('name', '') for ing in recipe.get('missedIngredients', [])]
                
                # Eksik malzemeleri filtrele - çok temel olanları çıkar
                essential_missing = self._filter_essential_missing(missing_products)
                
                # Türkçe çevirileri yap
                title_tr = translate_text(recipe.get('title', ''), "en", "tr")
                used_products_tr = [translate_text(product, "en", "tr") for product in used_products[:3]]
                missing_products_tr = [translate_text(product, "en", "tr") for product in essential_missing]
                
                recommendations.append(RecipeRecommendation(
                    recipe_id=recipe.get('id', 0),
                    title=recipe.get('title', ''),
                    title_tr=title_tr,
                    image=recipe.get('image', ''),
                    ready_in_minutes=recipe.get('readyInMinutes', 0),
                    servings=recipe.get('servings', 0),
                    source_url=recipe.get('sourceUrl', '') or f"https://spoonacular.com/recipes/{recipe.get('id', '')}" or "https://spoonacular.com/",
                    used_products=used_products,
                    used_products_tr=used_products_tr,
                    missing_products=essential_missing,
                    missing_products_tr=missing_products_tr,
                    priority_score=priority_score,
                    shelf_life_urgency=urgency,
                    instructions=recipe.get('instructions', ''),
                    summary=recipe.get('summary', '')
                ))
        
        # Öncelik skoruna göre sırala
        recommendations.sort(key=lambda x: x.priority_score, reverse=True)
        
        return recommendations[:max_recipes]
    
    def recommend_recipes(self, user_id: int = 1, max_recipes: int = 10) -> List[RecipeRecommendation]:
        """Kullanıcının envanterinden tarif öner"""
        logger.info(f"👤 Kullanıcı {user_id} için envanterden tarif önerisi")
        
        try:
            # Kullanıcının envanterini al
            inventory = self._get_user_inventory(user_id)
            if not inventory:
                logger.warning("❌ Envanter boş")
                return []
            
            # Envanterden malzeme isimlerini çıkar
            ingredients = []
            for item in inventory:
                try:
                    # Önce name_en'i kontrol et (temiz İngilizce)
                    if item.name_en and len(item.name_en.strip()) > 2:
                        ingredients.append(item.name_en.strip())
                        logger.info(f"✅ İngilizce isim kullanıldı: {item.name_en}")
                    else:
                        # name_en yoksa name_tr'yi çevir
                        raw_name = item.name_tr if hasattr(item, 'name_tr') else str(item)
                        
                        # Ham fiş metni karakterlerini temizle
                        if any(char in raw_name for char in ['*', 'x', '#', '408', '443', 'x3,45', 'x2,00', 'x1,15']):
                            # Basit normalizasyon
                            normalized_name = raw_name
                            # Sayıları ve özel karakterleri temizle
                            import re
                            normalized_name = re.sub(r'[xX]\d+[,.]?\d*', '', normalized_name)  # x3,45 gibi
                            normalized_name = re.sub(r'\d+[,.]?\d*', '', normalized_name)  # Sayılar
                            normalized_name = re.sub(r'[*#]', '', normalized_name)  # Özel karakterler
                            normalized_name = re.sub(r'\s+', ' ', normalized_name).strip()  # Fazla boşluklar
                            
                            # Çok kısa veya anlamsız isimleri atla
                            if len(normalized_name) < 3 or normalized_name in ['', ' ', 'ii', 'pi', 'a', 'e']:
                                continue
                                
                            # İngilizceye çevir
                            from translate_utils import translate_text
                            translated_name = translate_text(normalized_name)
                            if translated_name and len(translated_name) > 2:
                                ingredients.append(translated_name)
                                logger.info(f"✅ Temizlendi: '{raw_name}' -> '{translated_name}'")
                            else:
                                logger.warning(f"❌ Çevrilemedi: '{normalized_name}'")
                        else:
                            # Temiz isimse direkt çevir
                            from translate_utils import translate_text
                            translated_name = translate_text(raw_name)
                            if translated_name and len(translated_name) > 2:
                                ingredients.append(translated_name)
                                logger.info(f"✅ Temiz isim: '{raw_name}' -> '{translated_name}'")
                                
                except Exception as e:
                    logger.warning(f"❌ İşleme hatası: {e}")
                    continue
            
            logger.info(f"📦 Envanter malzemeleri: {ingredients}")
            
            # Spoonacular API'den tarif ara
            recipes = self.search_recipes_by_ingredients(ingredients, user_id=user_id)
            if not recipes:
                logger.warning("❌ Envanterden tarif bulunamadı")
                return []
            
            # Detaylı bilgileri al
            detailed_recipes = self._get_detailed_recipes([r['id'] for r in recipes])
            
            # Tarifleri önceliklendir
            recommendations = []
            for recipe in detailed_recipes:
                priority_score, urgency = self.calculate_recipe_priority(recipe, inventory)
                
                if priority_score >= 0:  # Tüm tarifleri al
                    used_products = [ing.get('name', '') for ing in recipe.get('usedIngredients', [])]
                    missing_products = [ing.get('name', '') for ing in recipe.get('missedIngredients', [])]
                    
                    # Türkçe çevirileri yap
                    try:
                        title_tr = translate_utils.translate_text(recipe.get('title', ''))
                        used_products_tr = [translate_utils.translate_text(product) for product in used_products[:3]]
                        missing_products_tr = [translate_utils.translate_text(product) for product in missing_products]
                    except Exception as e:
                        logger.warning(f"Çeviri hatası: {e}")
                        title_tr = recipe.get('title', '')
                        used_products_tr = used_products[:3]
                        missing_products_tr = missing_products
                    
                    recommendations.append(RecipeRecommendation(
                        recipe_id=recipe.get('id', 0),
                        title=recipe.get('title', ''),
                        title_tr=title_tr,
                        used_products=used_products[:3],
                        used_products_tr=used_products_tr,
                        missing_products=missing_products,
                        missing_products_tr=missing_products_tr,
                        priority_score=priority_score,
                        shelf_life_urgency=urgency,
                        ready_in_minutes=recipe.get('readyInMinutes', 0),
                        servings=recipe.get('servings', 0),
                        source_url=recipe.get('sourceUrl', '') or f"https://spoonacular.com/recipes/{recipe.get('id', '')}" or "https://spoonacular.com/",
                        instructions=recipe.get('instructions', ''),
                        summary=recipe.get('summary', '')
                    ))
            
            # Öncelik skoruna göre sırala
            recommendations.sort(key=lambda x: x.priority_score, reverse=True)
            
            logger.info(f"✅ {len(recommendations)} envanter tarifi hazırlandı")
            return recommendations[:max_recipes]
            
        except Exception as e:
            logger.error(f"❌ Envanterden tarif önerisi hatası: {e}")
            return []
    
    def _get_user_inventory(self, user_id: int) -> List[ProductWithShelfLife]:
        """Kullanıcının envanterini veritabanından al"""
        try:
            engine = get_engine()
            with engine.connect() as conn:
                # PostgreSQL için sorgu
                result = conn.execute(text("""
                    SELECT id, product_name, product_name_en, category_id, 
                           purchase_date, expiry_date, shelf_life_days
                    FROM user_inventory 
                    WHERE user_id = :user_id AND expiry_date > NOW()
                    ORDER BY expiry_date ASC
                """), {"user_id": user_id})
                
                inventory_items = result.fetchall()
                inventory_products = []
                
                for item in inventory_items:
                    # Raf ömrü hesapla
                    expiry_date = item.expiry_date
                    days_remaining = (expiry_date - datetime.now()).days
                    
                    # Öncelik skoru hesapla (raf ömrüne göre)
                    if days_remaining <= 3:
                        priority_score = 90  # Çok acil
                    elif days_remaining <= 7:
                        priority_score = 70  # Acil
                    elif days_remaining <= 14:
                        priority_score = 50  # Orta
                    else:
                        priority_score = 30  # Normal
                    
                    # Ürün adını belirle (İngilizce varsa onu kullan)
                    name_en = item.product_name_en if hasattr(item, 'product_name_en') and item.product_name_en else item.product_name
                    name_tr = item.product_name if hasattr(item, 'product_name') else str(item)
                    
                    inventory_products.append(ProductWithShelfLife(
                        product_id=item.id,
                        name_en=name_en,
                        name_tr=name_tr,
                        days_remaining=days_remaining,
                        storage_type="pantry",
                        open_state="sealed",
                        priority_score=priority_score
                    ))
                
                logger.info(f"📦 Kullanıcı {user_id} envanteri: {len(inventory_products)} ürün")
                return inventory_products
                
        except Exception as e:
            logger.error(f"❌ Envanter alma hatası: {e}")
            # Hata durumunda boş liste döndür
            return []
        """Kullanıcının envanterinden tarif öner"""
        logger.info(f"👤 Kullanıcı {user_id} için envanterden tarif önerisi")
        
        try:
            # Kullanıcının envanterini al
            inventory_products = self._get_user_inventory(user_id)
            
            if not inventory_products:
                logger.warning("❌ Kullanıcının envanteri boş!")
                return []
            
            # Envanter ürünlerini ingredient listesine dönüştür (zaten normalize edilmiş)
            ingredients = []
            for product in inventory_products[:10]:
                if product.name_en and len(product.name_en.strip()) > 0:
                    ingredients.append(product.name_en)
                else:
                    # name_tr'yi İngilizceye çevir
                    try:
                        from translate_utils import translate_text
                        translated_name = translate_text(product.name_tr)
                        if translated_name:
                            ingredients.append(translated_name)
                        else:
                            ingredients.append(product.name_tr)
                    except Exception as e:
                        logger.warning(f"Çeviri hatası {product.name_tr}: {e}")
                        ingredients.append(product.name_tr)
            
            logger.info(f"📦 Envanterden {len(ingredients)} ürün alındı: {ingredients[:5]}")
            
            # Tarifleri ara
            recipes = self.search_recipes_by_ingredients(ingredients, max_recipes * 2, user_id)
            
            if not recipes:
                logger.warning("❌ Tarif bulunamadı!")
                return []
            
            # Detaylı bilgileri al
            detailed_recipes = self._get_detailed_recipes(recipes)
            
            # Önceliklendirme yap
            recommendations = []
            for recipe_data in detailed_recipes:
                recommendation = RecipeRecommendation(
                    recipe_id=recipe_data['id'],
                    title=recipe_data['title'],
                    title_tr=recipe_data['title'],
                    image=recipe_data.get('image', ''),
                    ready_in_minutes=recipe_data.get('readyInMinutes', 0),
                    servings=recipe_data.get('servings', 1),
                    source_url=recipe_data.get('sourceUrl', ''),
                    used_products=recipe_data.get('usedIngredients', []),
                    used_products_tr=recipe_data.get('usedIngredients', []),
                    missing_products=recipe_data.get('missedIngredients', []),
                    missing_products_tr=recipe_data.get('missedIngredients', []),
                    priority_score=75.0,  # Varsayılan skor
                    shelf_life_urgency="DÜŞÜK",
                    instructions=recipe_data.get('instructions', ''),
                    summary=recipe_data.get('summary', '')
                )
                recommendations.append(recommendation)
            
            logger.info(f"✅ {len(recommendations)} tarif önerisi hazırlandı")
            return recommendations[:max_recipes]
            
        except Exception as e:
            logger.error(f"❌ Envanterden tarif önerisi hatası: {e}")
            return []
    
    def display_recommendations(self, recommendations: List[RecipeRecommendation]):
        """Önerileri göster"""
        if not recommendations:
            print("❌ Tarif önerisi bulunamadı!")
            return
        
        print(f"\n🍽️  TARİF ÖNERİLERİ (Raf Ömrüne Göre Önceliklendirilmiş)")
        print("=" * 80)
        
        for i, rec in enumerate(recommendations, 1):
            urgency_emoji = {
                "çok_acil": "🔴",
                "acil": "🟠", 
                "orta": "🟡",
                "düşük": "🟢",
                "çok_düşük": "⚪"
            }.get(rec.shelf_life_urgency, "⚪")
            
            print(f"\n{i}. {rec.title_tr}")
            print(f"   {urgency_emoji} Öncelik: {rec.priority_score:.1f} | {rec.shelf_life_urgency.upper()}")
            print(f"   ⏱️  Hazırlanma: {rec.ready_in_minutes} dk | 👥 Porsiyon: {rec.servings}")
            print(f"   🍽️  Kullanılan: {', '.join(rec.used_products_tr[:3])}{'...' if len(rec.used_products_tr) > 3 else ''}")
            if rec.missing_products_tr:
                print(f"   ❌ Eksik: {', '.join(rec.missing_products_tr[:2])}{'...' if len(rec.missing_products_tr) > 2 else ''}")
            print(f"   🔗 {rec.source_url}")

def main():
    """Ana fonksiyon"""
    logging.basicConfig(level=logging.INFO)
    
    print("🍽️  Raf Ömrüne Göre Tarif Önerisi Sistemi")
    print("=" * 50)
    
    try:
        recommender = RecipeRecommender()
        recommendations = recommender.recommend_recipes(user_id=1, max_recipes=5)
        recommender.display_recommendations(recommendations)
        
    except Exception as e:
        print(f"❌ Hata: {e}")

if __name__ == "__main__":
    main()