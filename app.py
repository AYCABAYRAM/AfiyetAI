# app.py
# -*- coding: utf-8 -*-
"""
AfiyetAI Web Arayüzü - Fiş Yükleme ve Tarif Önerisi
"""
import os
import logging
import json
from datetime import datetime
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, session, send_from_directory
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import tempfile
import shutil
import sqlite3
import uuid
from dotenv import load_dotenv

# .env dosyasını yükle
load_dotenv()
from functools import wraps
import time
from collections import defaultdict
from sqlalchemy import text

from receipt_ingest import process_receipt_image
from recipe_recommender import RecipeRecommender
import translate_utils

def generate_mock_recipes_from_receipt(ingredients):
    """Spoonacular API olmadan receipt ürünlerine göre tarif üret"""
    mock_recipes = []
    
    # Basit kategori eşleştirmesi
    ingredients_lower = [i.lower() for i in ingredients]
    
    if any(word in ' '.join(ingredients_lower) for word in ['tavuk', 'chicken', 'pil']):
        mock_recipes.append({
            'title_tr': 'Tavuklu vejetaryen Tarif',
            'priority_score': 85.0,
            'shelf_life_urgency': 'ORTA',
            'ready_in_minutes': 30,
            'servings': 4,
            'used_products_tr': ['tavuk'],
            'missing_products_tr': ['baharat'],
            'source_url': 'mock://recipe/tavuk'
        })
    
    if any(word in ' '.join(ingredients_lower) for word in ['peynir', 'cheese']):
        mock_recipes.append({
            'title_tr': 'Fırında Peynirli Tarif',
            'priority_score': 78.0,
            'shelf_life_urgency': 'ACIL',
            'ready_in_minutes': 25,
            'servings': 2,
            'used_products_tr': ['peynir'],
            'missing_products_tr': ['un'],
            'source_url': 'mock://recipe/peynir'
        })
    
    if any(word in ' '.join(ingredients_lower) for word in ['fesleğen', 'basil', 'fesle']):
        mock_recipes.append({
            'title_tr': 'Taze Fesleğen Soslu Makarna',
            'priority_score': 92.0,
            'shelf_life_urgency': 'ÇOK_ACİL',
            'ready_in_minutes': 15,
            'servings': 3,
            'used_products_tr': ['fesleğen'],
            'missing_products_tr': ['makarna'],
            'source_url': 'mock://recipe/feslek'
        })
    
    # Fiş malzemelerine göre basit tarif ekle
    if ingredients:
        mock_recipes.append({
            'title_tr': f'{ingredients[0][:15]} ile Hızlı Tarif',
            'priority_score': 75.0,
            'shelf_life_urgency': 'DÜŞÜK',
            'ready_in_minutes': 20,
            'servings': 2,
            'used_products_tr': ingredients[:2],
            'missing_products_tr': ['tuz', 'biber'],
            'source_url': 'mock://recipe/fiested'
        })
    
    return mock_recipes[:5]

# ======== Normalize isim seçici yardımcılar (EKLENDİ) ========
def pick_tr_name(p: dict) -> str:
    return (
        p.get('normalized_text_tr')
        or p.get('canonical_name_tr')
        or p.get('name_tr')
        or p.get('name')
        or ''
    )

def pick_en_name(p: dict) -> str:
    return (
        p.get('normalized_text_en')
        or p.get('canonical_name_en')
        or p.get('name_en')
        or ''
    )
# =============================================================

# Flask uygulaması
app = Flask(__name__)
# Güvenli secret key - production'da environment variable'dan alınmalı
import secrets
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))

# Production güvenlik ayarları
if os.environ.get('FLASK_ENV') == 'production':
    app.config['SESSION_COOKIE_SECURE'] = os.environ.get('SESSION_COOKIE_SECURE', 'True').lower() == 'true'
    app.config['SESSION_COOKIE_HTTPONLY'] = os.environ.get('SESSION_COOKIE_HTTPONLY', 'True').lower() == 'true'
    app.config['SESSION_COOKIE_SAMESITE'] = os.environ.get('SESSION_COOKIE_SAMESITE', 'Lax')
    app.config['PERMANENT_SESSION_LIFETIME'] = int(os.environ.get('SESSION_LIFETIME', 7200))  # Default: 2 saat

# Dosya yükleme ayarları
UPLOAD_FOLDER = os.environ.get('UPLOAD_FOLDER', 'uploads')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'bmp', 'tiff'}
MAX_FILE_SIZE = int(os.environ.get('MAX_FILE_SIZE', 16 * 1024 * 1024))  # Default: 16MB

# Klasörü oluştur
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = MAX_FILE_SIZE
app.config['DATABASE'] = os.environ.get('SQLITE_DATABASE', 'users.db')
app.config['PERMANENT_SESSION_LIFETIME'] = int(os.environ.get('SESSION_LIFETIME', 3600))  # Default: 1 saat

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Uploads klasörü için route
@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# Veritabanı bağlantısı
def get_db_connection():
    """PostgreSQL veritabanı bağlantısı"""
    import psycopg2
    
    DATABASE_URL = os.environ.get('DATABASE_URL')
    if DATABASE_URL and DATABASE_URL.startswith('postgresql'):
        # PostgreSQL connection - DATABASE_URL'den parse et
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    else:
        # Fallback to SQLite
        conn = sqlite3.connect(app.config['DATABASE'])
        conn.row_factory = sqlite3.Row
        return conn

def init_db():
    """Veritabanını başlat"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    DATABASE_URL = os.environ.get('DATABASE_URL')
    if DATABASE_URL and DATABASE_URL.startswith('postgresql'):
        # PostgreSQL - create user_inventory table if not exists
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_inventory (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                product_name TEXT NOT NULL,
                product_name_en TEXT,
                category_id INTEGER,
                quantity DECIMAL(10,2),
                unit TEXT,
                purchase_date TIMESTAMP DEFAULT NOW(),
                expiry_date TIMESTAMP,
                shelf_life_days INTEGER,
                source_receipt_id INTEGER,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        ''')
        
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_user_inventory_user_id 
            ON user_inventory(user_id)
        ''')
        
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_user_inventory_expiry 
            ON user_inventory(expiry_date)
        ''')
        
        conn.commit()
        cursor.close()
        conn.close()
    else:
        # SQLite - create tables
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                premium BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                session_token TEXT UNIQUE NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_inventory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                product_name TEXT NOT NULL,
                product_name_en TEXT,
                category_id INTEGER,
                quantity DECIMAL(10,2),
                unit TEXT,
                purchase_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expiry_date TIMESTAMP,
                shelf_life_days INTEGER,
                source_receipt_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        ''')
        
        conn.commit()
        cursor.close()
        conn.close()

# Veritabanını başlat
init_db()

# Envanter yönetimi fonksiyonları
def add_products_to_inventory(user_id, products, receipt_id=None):
    """Fişten çıkarılan ürünleri kullanıcı envanterine ekle"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        added_count = 0
        for product in products:
            # Ürün bilgilerini al (NORMALIZE ÖNCELİKLİ)  >>> EKLENDİ <<<
            product_name = (
                product.get('normalized_text_tr')
                or product.get('canonical_name_tr')
                or product.get('name_tr')
                or product.get('name', '')
            )
            product_name_en = (
                product.get('normalized_text_en')
                or product.get('canonical_name_en')
                or product.get('name_en', '')
            )
            category_id = product.get('category_id')
            shelf_life_days = product.get('shelf_life_days', 7)
            
            # Raf ömrü hesapla
            from datetime import datetime, timedelta
            purchase_date = datetime.now()
            expiry_date = purchase_date + timedelta(days=shelf_life_days)
            
            DATABASE_URL = os.environ.get('DATABASE_URL')
            if DATABASE_URL and DATABASE_URL.startswith('postgresql'):
                cursor.execute('''
                    INSERT INTO user_inventory 
                    (user_id, product_name, product_name_en, category_id, quantity, 
                     purchase_date, expiry_date, shelf_life_days, source_receipt_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ''', (user_id, product_name, product_name_en, category_id, 1.0,
                      purchase_date, expiry_date, shelf_life_days, receipt_id))
            else:
                cursor.execute('''
                    INSERT INTO user_inventory 
                    (user_id, product_name, product_name_en, category_id, quantity, 
                     purchase_date, expiry_date, shelf_life_days, source_receipt_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (user_id, product_name, product_name_en, category_id, 1.0,
                      purchase_date, expiry_date, shelf_life_days, receipt_id))
            
            added_count += 1
        
        conn.commit()
        logger.info(f"✅ {added_count} ürün kullanıcı {user_id} envanterine eklendi")
        return added_count
        
    except Exception as e:
        logger.error(f"❌ Envanter ekleme hatası: {e}")
        conn.rollback()
        return 0
    finally:
        cursor.close()
        conn.close()

def get_user_inventory(user_id, include_expired=False):
    """Kullanıcının aktif envanterini getir"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        DATABASE_URL = os.environ.get('DATABASE_URL')
        if DATABASE_URL and DATABASE_URL.startswith('postgresql'):
            if include_expired:
                cursor.execute('''
                    SELECT * FROM user_inventory 
                    WHERE user_id = %s 
                    ORDER BY expiry_date ASC
                ''', (user_id,))
            else:
                cursor.execute('''
                    SELECT * FROM user_inventory 
                    WHERE user_id = %s AND expiry_date > NOW()
                    ORDER BY expiry_date ASC
                ''', (user_id,))
        else:
            if include_expired:
                cursor.execute('''
                    SELECT * FROM user_inventory 
                    WHERE user_id = ? 
                    ORDER BY expiry_date ASC
                ''', (user_id,))
            else:
                cursor.execute('''
                    SELECT * FROM user_inventory 
                    WHERE user_id = ? AND expiry_date > datetime('now')
                    ORDER BY expiry_date ASC
                ''', (user_id,))
        
        inventory = cursor.fetchall()
        logger.info(f"📦 Kullanıcı {user_id} envanteri: {len(inventory)} ürün")
        return inventory
        
    except Exception as e:
        logger.error(f"❌ Envanter getirme hatası: {e}")
        return []
    finally:
        cursor.close()
        conn.close()

def clean_expired_inventory(user_id=None):
    """Süresi dolmuş ürünleri temizle"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        DATABASE_URL = os.environ.get('DATABASE_URL')
        if DATABASE_URL and DATABASE_URL.startswith('postgresql'):
            if user_id:
                cursor.execute('''
                    DELETE FROM user_inventory 
                    WHERE user_id = %s AND expiry_date <= NOW()
                ''', (user_id,))
            else:
                cursor.execute('''
                    DELETE FROM user_inventory 
                    WHERE expiry_date <= NOW()
                ''')
        else:
            if user_id:
                cursor.execute('''
                    DELETE FROM user_inventory 
                    WHERE user_id = ? AND expiry_date <= datetime('now')
                ''', (user_id,))
            else:
                cursor.execute('''
                    DELETE FROM user_inventory 
                    WHERE expiry_date <= datetime('now')
                ''')
        
        deleted_count = cursor.rowcount
        conn.commit()
        
        if deleted_count > 0:
            logger.info(f"🗑️ {deleted_count} süresi dolmuş ürün temizlendi")
        
        return deleted_count
        
    except Exception as e:
        logger.error(f"❌ Envanter temizleme hatası: {e}")
        conn.rollback()
        return 0
    finally:
        cursor.close()
        conn.close()

def get_user_receipt_count(user_id):
    """Kullanıcının fiş sayısını getir"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        DATABASE_URL = os.environ.get('DATABASE_URL')
        if DATABASE_URL and DATABASE_URL.startswith('postgresql'):
            cursor.execute('''
                SELECT COUNT(DISTINCT source_receipt_id) 
                FROM user_inventory 
                WHERE user_id = %s AND source_receipt_id IS NOT NULL
            ''', (user_id,))
        else:
            cursor.execute('''
                SELECT COUNT(DISTINCT source_receipt_id) 
                FROM user_inventory 
                WHERE user_id = ? AND source_receipt_id IS NOT NULL
            ''', (user_id,))
        
        result = cursor.fetchone()
        count = result[0] if result else 0
        
        logger.info(f"📊 Kullanıcı {user_id} fiş sayısı: {count}")
        return count
        
    except Exception as e:
        logger.error(f"❌ Fiş sayısı alma hatası: {e}")
        return 0
    finally:
        cursor.close()
        conn.close()

# Süresi dolmuş ürünleri temizle
clean_expired_inventory()

# Rate limiting için basit sistem
upload_attempts = defaultdict(list)
MAX_UPLOADS_PER_HOUR = int(os.environ.get('MAX_UPLOADS_PER_HOUR', 10))  # Saatte maksimum fiş yükleme

def check_rate_limit():
    """Rate limiting kontrolü"""
    user_id = session.get('user_id')
    if not user_id:
        return True
    
    current_time = time.time()
    # Son 1 saatteki yüklemeleri temizle
    upload_attempts[user_id] = [
        attempt_time for attempt_time in upload_attempts[user_id] 
        if current_time - attempt_time < 3600
    ]
    
    # Limit kontrolü
    if len(upload_attempts[user_id]) >= MAX_UPLOADS_PER_HOUR:
        return False
    
    # Yeni yüklemeyi kaydet
    upload_attempts[user_id].append(current_time)
    return True

def login_required(f):
    """Giriş yapmış kullanıcı kontrolü"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Bu sayfaya erişmek için giriş yapmalısınız!', 'error')
            return redirect(url_for('login'))
        
        # Kullanıcının gerçekten var olduğunu kontrol et
        user = get_current_user()
        if not user:
            session.clear()
            flash('Oturum süresi dolmuş. Lütfen tekrar giriş yapın.', 'error')
            return redirect(url_for('login'))
        
        return f(*args, **kwargs)
    return decorated_function

def get_current_user():
    """Mevcut kullanıcıyı getir"""
    if 'user_id' not in session:
        return None
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    DATABASE_URL = os.environ.get('DATABASE_URL')
    if DATABASE_URL and DATABASE_URL.startswith('postgresql'):
        # PostgreSQL
        cursor.execute('SELECT user_id, email, display_name, password_hash FROM users WHERE user_id = %s', (session['user_id'],))
        user = cursor.fetchone()
        if user:
            # PostgreSQL tuple: (user_id, email, display_name, password_hash)
            user_dict = {
                'user_id': user[0],
                'email': user[1], 
                'display_name': user[2],
                'username': user[2],
                'password_hash': user[3]
            }
            user = user_dict
    else:
        # SQLite
        cursor.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],))
        user_row = cursor.fetchone()
        if user_row:
            # SQLite row: (id, username, email, password_hash, premium, created_at)
            user = {
                'user_id': user_row[0],
                'id': user_row[0],
                'username': user_row[1],
                'display_name': user_row[1],
                'email': user_row[2],
                'password_hash': user_row[3],
                'premium': user_row[4]
            }
    
    cursor.close()
    conn.close()
    return user

def allowed_file(filename):
    """Dosya uzantısı kontrolü"""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/')
def index():
    """Ana sayfa"""
    user = get_current_user()
    return render_template('index.html', user=user)

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Kullanıcı girişi"""
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        DATABASE_URL = os.environ.get('DATABASE_URL')
        if DATABASE_URL and DATABASE_URL.startswith('postgresql'):
            # PostgreSQL
            cursor.execute('SELECT user_id, email, display_name, password_hash FROM users WHERE display_name = %s', (username,))
            user = cursor.fetchone()
            if user:
                # PostgreSQL tuple: (user_id, email, display_name, password_hash)
                user_dict = {
                    'user_id': user[0],
                    'email': user[1], 
                    'display_name': user[2],
                    'password_hash': user[3]
                }
                user = user_dict
        else:
            # SQLite
            cursor.execute('SELECT * FROM users WHERE username = ?', (username,))
            user_row = cursor.fetchone()
            if user_row:
                # SQLite row: (id, username, email, password_hash, premium, created_at)
                user = {
                    'user_id': user_row[0],
                    'username': user_row[1],
                    'display_name': user_row[1],  # username'i display_name olarak kullan
                    'email': user_row[2],
                    'password_hash': user_row[3]
                }
        
        cursor.close()
        conn.close()
        
        if user and check_password_hash(user['password_hash'], password):
            session['user_id'] = user['user_id']
            session['username'] = user['display_name']
            session.permanent = True  # Oturum süresini aktif et
            flash('Başarıyla giriş yaptınız!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Kullanıcı adı veya şifre hatalı!', 'error')
    
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    """Kullanıcı kaydı"""
    if request.method == 'POST':
        username = request.form['username']
        email = request.form['email']
        password = request.form['password']
        
        if not username or not email or not password:
            flash('Tüm alanları doldurun!', 'error')
            return render_template('register.html')
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        DATABASE_URL = os.environ.get('DATABASE_URL')
        if DATABASE_URL and DATABASE_URL.startswith('postgresql'):
            # PostgreSQL
            # Kullanıcı adı kontrolü
            cursor.execute('SELECT user_id FROM users WHERE display_name = %s', (username,))
            existing_user = cursor.fetchone()
            if existing_user:
                flash('Bu kullanıcı adı zaten kullanılıyor!', 'error')
                cursor.close()
                conn.close()
                return render_template('register.html')
            
            # Email kontrolü
            cursor.execute('SELECT user_id FROM users WHERE email = %s', (email,))
            existing_email = cursor.fetchone()
            if existing_email:
                flash('Bu email adresi zaten kullanılıyor!', 'error')
                cursor.close()
                conn.close()
                return render_template('register.html')
            
            # Yeni kullanıcı oluştur
            password_hash = generate_password_hash(password)
            cursor.execute('INSERT INTO users (display_name, email, password_hash, created_at, updated_at) VALUES (%s, %s, %s, NOW(), NOW())',
                        (username, email, password_hash))
            conn.commit()
            cursor.close()
            conn.close()
        else:
            # SQLite
            # Kullanıcı adı kontrolü
            cursor.execute('SELECT id FROM users WHERE username = ?', (username,))
            existing_user = cursor.fetchone()
            if existing_user:
                flash('Bu kullanıcı adı zaten kullanılıyor!', 'error')
                cursor.close()
                conn.close()
                return render_template('register.html')
            
            # Email kontrolü
            cursor.execute('SELECT id FROM users WHERE email = ?', (email,))
            existing_email = cursor.fetchone()
            if existing_email:
                flash('Bu email adresi zaten kullanılıyor!', 'error')
                cursor.close()
                conn.close()
                return render_template('register.html')
            
            # Yeni kullanıcı oluştur
            password_hash = generate_password_hash(password)
            cursor.execute('INSERT INTO users (username, email, password_hash) VALUES (?, ?, ?)',
                        (username, email, password_hash))
            conn.commit()
            cursor.close()
            conn.close()
        
        flash('Kayıt başarılı! Şimdi giriş yapabilirsiniz.', 'success')
        return redirect(url_for('login'))
    
    return render_template('register.html')

@app.route('/logout')
def logout():
    """Çıkış yap"""
    session.clear()
    flash('Başarıyla çıkış yaptınız!', 'success')
    return redirect(url_for('index'))

@app.route('/dashboard')
@login_required
def dashboard():
    """Kullanıcı paneli"""
    user = get_current_user()
    user_id = user.get('user_id') or user.get('id')
    
    # Kullanıcının envanterini getir
    inventory = get_user_inventory(user_id, include_expired=False)
    
    # Kullanıcının fiş sayısını getir
    receipt_count = get_user_receipt_count(user_id)
    
    # Şu anki tarih
    from datetime import datetime
    now = datetime.now()
    
    
    return render_template('dashboard.html', 
                         user=user, 
                         inventory=inventory, 
                         receipt_count=receipt_count, 
                         now=now,
)

@app.route('/api/inventory')
@login_required
def api_inventory():
    """Kullanıcı envanterini JSON olarak döndür"""
    user = get_current_user()
    user_id = user.get('user_id') or user.get('id')
    
    inventory = get_user_inventory(user_id, include_expired=False)
    
    # Envanteri JSON formatına dönüştür
    inventory_data = []
    for item in inventory:
        inventory_data.append({
            'id': item[0] if isinstance(item, tuple) else item.get('id'),
            'product_name': item[2] if isinstance(item, tuple) else item.get('product_name'),
            'product_name_en': item[3] if isinstance(item, tuple) else item.get('product_name_en'),
            'category_id': item[4] if isinstance(item, tuple) else item.get('category_id'),
            'quantity': float(item[5]) if isinstance(item, tuple) else float(item.get('quantity', 1)),
            'unit': item[6] if isinstance(item, tuple) else item.get('unit'),
            'purchase_date': item[7].isoformat() if isinstance(item, tuple) else item.get('purchase_date'),
            'expiry_date': item[8].isoformat() if isinstance(item, tuple) else item.get('expiry_date'),
            'shelf_life_days': item[9] if isinstance(item, tuple) else item.get('shelf_life_days'),
            'source_receipt_id': item[10] if isinstance(item, tuple) else item.get('source_receipt_id')
        })
    
    return jsonify({
        'success': True,
        'inventory': inventory_data,
        'total_items': len(inventory_data)
    })

@app.route('/upload', methods=['POST'])
@login_required
def upload_file():
    """Fiş yükleme ve işleme"""
    try:
        # Rate limiting kontrolü
        if not check_rate_limit():
            flash('Çok fazla fiş yüklediniz. Lütfen 1 saat sonra tekrar deneyin.', 'error')
            return redirect(url_for('index'))
        
        if 'file' not in request.files:
            flash('Dosya seçilmedi!', 'error')
            return redirect(url_for('index'))
        
        file = request.files['file']
        if file.filename == '':
            flash('Dosya seçilmedi!', 'error')
            return redirect(url_for('index'))
        
        if file and allowed_file(file.filename):
            # Güvenli dosya adı
            filename = secure_filename(file.filename)
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"{timestamp}_{filename}"
            
            # Dosyayı kaydet
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)
            
            logger.info(f"Fiş yüklendi: {filename}")
            
            # Fişi işle
            user = get_current_user()
            user_id = user.get('user_id') or user.get('id')
            result = process_receipt_image(filepath, user_id=user_id)
            
            if result and result.get('success'):
                # Fişten çıkarılan ürünleri al
                receipt_products = result.get('products', [])
                logger.info(f"📦 Fişten {len(receipt_products)} ürün alındı")
                
                # Bu ürünleri tarif önerisi için RecipeRecommender'a geç
                recommender = RecipeRecommender()
                
                # Receipt ürünlerini tarif önerisi için ingredient listesine dönüştür (NORMALIZE KULLAN) >>> EKLENDİ <<<
                extracted_ingredients = []
                for product in receipt_products[:10]:  # En fazla 10 ürün
                    product_name_en = pick_en_name(product).strip()  # ⭐ İNGİLİZCE isim kullan!
                    if product_name_en and len(product_name_en) > 2:
                        extracted_ingredients.append(product_name_en)
                
                logger.info(f"🔍 Fişten çıkarılan ürünler: {extracted_ingredients}")
                
                # Eğer fişte ürün bulunamadıysa normal user inventory kullan
                if not extracted_ingredients:
                    logger.info("❌ Fişte ürün bulunamadı, inventory kullanılıyor")
                    recommendations = recommender.recommend_recipes(user_id=user_id, max_recipes=5)
                else:
                    # Fiş ürünlerini kullanarak tarif ara
                    logger.info(f"🍽️ Fiş ürünleriyle tarif aranıyor: {extracted_ingredients[:5]}")
                    try:
                        recommendations = recommender.recommend_recipes_from_receipt(extracted_ingredients[:5], max_recipes=5, user_id=user_id)
                        logger.info(f"✅ Fiş ürünleriyle {len(recommendations)} tarif bulundu")
                        if not recommendations:
                            logger.info("❌ Spoonacular API problemi - Kullanıcı envanterinden tarif aranıyor")
                            # Kullanıcının mevcut envanterinden tarif öner
                            try:
                                recommendations = recommender.recommend_recipes(user_id=user_id, max_recipes=5)
                                logger.info(f"✅ Envanterden {len(recommendations)} tarif bulundu")
                            except Exception as e:
                                logger.error(f"❌ Envanter tarif arama hatası: {e}")
                                recommendations = []
                    except Exception as e:
                        logger.error(f"❌ Fiş ürünleriyle tarif arama hatası: {e}")
                        logger.info("🔧 Kullanıcı envanterinden tarif aranıyor")
                        # Kullanıcının mevcut envanterinden tarif öner
                        try:
                            recommendations = recommender.recommend_recipes(user_id=user_id, max_recipes=5)
                            logger.info(f"✅ Envanterden {len(recommendations)} tarif bulundu")
                        except Exception as e2:
                            logger.error(f"❌ Envanter tarif arama hatası: {e2}")
                            recommendations = []
                
                # Ürünleri kullanıcı envanterine ekle (ENVANTERE NORMALIZE YAZIYORUZ — yukarıda zaten düzeltildi)
                try:
                    receipt_id = result.get('receipt_id')
                    logger.info(f"🔍 Receipt ID: {receipt_id} (type: {type(receipt_id)})")
                    added_count = add_products_to_inventory(user_id, receipt_products, receipt_id)
                    logger.info(f"📦 {added_count} ürün envantere eklendi")
                except Exception as e:
                    logger.error(f"❌ Envanter ekleme hatası: {e}")
                
                # ŞABLONDA GÖRÜNEN İSİMLERİ NORMALIZE'A ZORLA >>> EKLENDİ <<<
                for p in receipt_products:
                    display_tr = pick_tr_name(p)
                    display_en = pick_en_name(p)
                    if display_tr:
                        p['name'] = display_tr
                    if display_en:
                        p['name_en'] = display_en

                # Sonuçları hazırla
                processed_data = {
                    'filename': filename,
                    'products': receipt_products,
                    'total_products': len(receipt_products),
                    'recommendations': recommendations,
                    'processing_time': result.get('processing_time', 0),
                    'receipt_image_url': f'/uploads/{filename}'  # Fiş görselini ekle
                }
                
                # Tarif önerilerini veritabanına kaydet
                try:
                    conn = get_db_connection()
                    cursor = conn.cursor()
                    
                    for i, rec in enumerate(recommendations):
                        cursor.execute("""
                            INSERT INTO Recipe_Recommendations 
                            (user_id, generated_at, model_version, context_json, top_k, recipe_id, score, shown_at)
                            VALUES (%s, NOW(), 'v1.0', %s, %s, %s, %s, NOW())
                        """, (
                            user_id,
                            json.dumps({
                                'receipt_products': extracted_ingredients[:5],
                                'recommendation_type': 'receipt_based'
                            }),
                            5,  # top_k
                            rec.recipe_id,
                            rec.priority_score
                        ))
                    
                    conn.commit()
                    cursor.close()
                    conn.close()
                    logger.info(f"✅ {len(recommendations)} tarif önerisi veritabanına kaydedildi")
                except Exception as e:
                    logger.error(f"❌ Tarif önerileri veritabanına kaydedilemedi: {e}")
                
                # Dosyayı kalıcı olarak sakla (fiş önizlemesi için)
                
                return render_template('results.html', data=processed_data)
            else:
                flash('Fiş işlenirken hata oluştu!', 'error')
                return redirect(url_for('index'))
        else:
            flash('Geçersiz dosya formatı! Sadece resim dosyaları kabul edilir.', 'error')
            return redirect(url_for('index'))
            
    except Exception as e:
        logger.error(f"Fiş yükleme hatası: {e}")
        flash(f'Hata: {str(e)}', 'error')
        return redirect(url_for('index'))

@app.route('/api/process', methods=['POST'])
@login_required
def api_process():
    """API endpoint - JSON response"""
    try:
        # Rate limiting kontrolü
        if not check_rate_limit():
            return jsonify({
                'success': False,
                'error': 'Çok fazla fiş yüklediniz. Lütfen 1 saat sonra tekrar deneyin.'
            }), 429
        
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'Dosya seçilmedi'})
        
        file = request.files['file']
        if file.filename == '':
            return jsonify({'success': False, 'error': 'Dosya seçilmedi'})
        
        if file and allowed_file(file.filename):
            # Geçici dosya oluştur
            with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as tmp_file:
                file.save(tmp_file.name)
                filepath = tmp_file.name
            
            # Fişi işle
            user = get_current_user()
            user_id = user.get('user_id') or user.get('id')
            result = process_receipt_image(filepath, user_id=user_id)
            
            if result and result.get('success'):
                # Fişten çıkarılan ürünleri al
                receipt_products = result.get('products', [])
                logger.info(f"📦 Fişten {len(receipt_products)} ürün alındı")
                
                # Bu ürünleri tarif önerisi için RecipeRecommender'a geç
                recommender = RecipeRecommender()
                
                # Receipt ürünlerini tarif önerisi için ingredient listesine dönüştür (NORMALIZE KULLAN) >>> EKLENDİ <<<
                extracted_ingredients = []
                for product in receipt_products[:10]:  # En fazla 10 ürün
                    product_name_en = pick_en_name(product).strip()  # ⭐ İNGİLİZCE isim kullan!
                    if product_name_en and len(product_name_en) > 2:
                        extracted_ingredients.append(product_name_en)
                
                logger.info(f"🔍 Fişten çıkarılan ürünler: {extracted_ingredients}")
                
                # Eğer fişte ürün bulunamadıysa normal user inventory kullan
                if not extracted_ingredients:
                    logger.info("❌ Fişte ürün bulunamadı, inventory kullanılıyor")
                    recommendations = recommender.recommend_recipes(user_id=user_id, max_recipes=5)
                else:
                    # Fiş ürünlerini kullanarak tarif ara
                    logger.info(f"🍽️ Fiş ürünleriyle tarif aranıyor: {extracted_ingredients[:5]}")
                    try:
                        recommendations = recommender.recommend_recipes_from_receipt(extracted_ingredients[:5], max_recipes=5, user_id=user_id)
                        logger.info(f"✅ Fiş ürünleriyle {len(recommendations)} tarif bulundu")
                        if not recommendations:
                            logger.info("❌ Spoonacular API problemi - Kullanıcı envanterinden tarif aranıyor")
                            # Kullanıcının mevcut envanterinden tarif öner
                            try:
                                recommendations = recommender.recommend_recipes(user_id=user_id, max_recipes=5)
                                logger.info(f"✅ Envanterden {len(recommendations)} tarif bulundu")
                            except Exception as e:
                                logger.error(f"❌ Envanter tarif arama hatası: {e}")
                                recommendations = []
                    except Exception as e:
                        logger.error(f"❌ Fiş ürünleriyle tarif arama hatası: {e}")
                        logger.info("🔧 Kullanıcı envanterinden tarif aranıyor")
                        # Kullanıcının mevcut envanterinden tarif öner
                        try:
                            recommendations = recommender.recommend_recipes(user_id=user_id, max_recipes=5)
                            logger.info(f"✅ Envanterden {len(recommendations)} tarif bulundu")
                        except Exception as e2:
                            logger.error(f"❌ Envanter tarif arama hatası: {e2}")
                            recommendations = []
                
                # Ürünleri kullanıcı envanterine ekle (ENVANTERE NORMALIZE YAZIYORUZ — yukarıda zaten düzeltildi)
                try:
                    receipt_id = result.get('receipt_id')
                    logger.info(f"🔍 Receipt ID: {receipt_id} (type: {type(receipt_id)})")
                    added_count = add_products_to_inventory(user_id, receipt_products, receipt_id)
                    logger.info(f"📦 {added_count} ürün envantere eklendi")
                except Exception as e:
                    logger.error(f"❌ Envanter ekleme hatası: {e}")
                
                # JSON'a dönecek ürün isimlerini normalize'a zorlama >>> EKLENDİ <<<
                for p in receipt_products:
                    display_tr = pick_tr_name(p)
                    display_en = pick_en_name(p)
                    if display_tr:
                        p['name'] = display_tr
                    if display_en:
                        p['name_en'] = display_en

                # Sonuçları hazırla
                response_data = {
                    'success': True,
                    'products': receipt_products,
                    'total_products': len(receipt_products),
                    'recommendations': [
                        {
                            'title': rec.title_tr,
                            'priority_score': rec.priority_score,
                            'urgency': rec.shelf_life_urgency,
                            'ready_in_minutes': rec.ready_in_minutes,
                            'servings': rec.servings,
                            'used_products': rec.used_products_tr,
                            'missing_products': rec.missing_products_tr,
                            'source_url': rec.source_url
                        }
                        for rec in recommendations
                    ],
                    'processing_time': result.get('processing_time', 0)
                }
                
                # Geçici dosyayı sil
                try:
                    os.remove(filepath)
                except:
                    pass
                
                return jsonify(response_data)
            else:
                return jsonify({'success': False, 'error': 'Fiş işlenirken hata oluştu'})
        else:
            return jsonify({'success': False, 'error': 'Geçersiz dosya formatı'})
            
    except Exception as e:
        logger.error(f"API işleme hatası: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/save-preferences', methods=['POST'])
@login_required
def save_preferences():
    """Kullanıcı tercihlerini kaydet"""
    try:
        user = get_current_user()
        data = request.get_json()
        
        if not data or 'type' not in data or 'items' not in data:
            return jsonify({'success': False, 'message': 'Geçersiz veri'})
        
        pref_type = data['type']
        items = data['items']
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        DATABASE_URL = os.environ.get('DATABASE_URL')
        if DATABASE_URL and DATABASE_URL.startswith('postgresql'):
            # PostgreSQL
            if pref_type == 'allergies':
                # Mevcut alerjileri sil
                cursor.execute("DELETE FROM user_allergies WHERE user_id = %s", (user['user_id'],))
            
                # Yeni alerjileri ekle
                for item in items:
                    # Alerji tablosunda var mı kontrol et
                    cursor.execute("SELECT allergen_id FROM allergens WHERE name = %s", (item,))
                    allergen_row = cursor.fetchone()
                    
                    if allergen_row:
                        allergen_id = allergen_row[0]
                    else:
                        # Yeni alerji ekle
                        cursor.execute("INSERT INTO allergens (name) VALUES (%s) RETURNING allergen_id", (item,))
                        allergen_id = cursor.fetchone()[0]
                    
                    # Kullanıcı alerjisini ekle
                    cursor.execute("INSERT INTO user_allergies (user_id, allergen_id) VALUES (%s, %s)", 
                                 (user["user_id"], allergen_id))
            
            elif pref_type == 'dislikes':
                # Mevcut sevilmeyenleri sil
                cursor.execute("DELETE FROM user_dislikes WHERE user_id = %s", (user['user_id'],))
                
                # Yeni sevilmeyenleri ekle
                for item in items:
                    # Ürün tablosunda var mı kontrol et
                    cursor.execute("SELECT product_id FROM products WHERE canonical_name_en = %s", (item,))
                    product_row = cursor.fetchone()
                    
                    if product_row:
                        product_id = product_row[0]
                        # Kullanıcı sevilmeyenini ekle
                        cursor.execute("INSERT INTO user_dislikes (user_id, product_id) VALUES (%s, %s)", 
                                     (user['user_id'], product_id))
            
            elif pref_type == 'diet-preferences':
                # Mevcut diyet tercihlerini sil
                cursor.execute("DELETE FROM user_dietary_preferences WHERE user_id = %s", (user['user_id'],))
                
                # Yeni diyet tercihlerini ekle
                for item in items:
                    # Diyet tercihi tablosunda var mı kontrol et
                    cursor.execute("SELECT pref_id FROM dietary_preferences WHERE label = %s", (item,))
                    pref_row = cursor.fetchone()
                    
                    if pref_row:
                        pref_id = pref_row[0]
                    else:
                        # Yeni diyet tercihi ekle
                        code = item.lower().replace(' ', '_')
                        cursor.execute("INSERT INTO dietary_preferences (code, label) VALUES (%s, %s) RETURNING pref_id", 
                                     (code, item))
                        pref_id = cursor.fetchone()[0]
                    
                    # Kullanıcı diyet tercihini ekle
                    cursor.execute("INSERT INTO user_dietary_preferences (user_id, pref_id) VALUES (%s, %s)", 
                                 (user['user_id'], pref_id))
        
            conn.commit()
            cursor.close()
            conn.close()
            
            return jsonify({'success': True, 'message': 'Tercihler başarıyla kaydedildi'})
        else:
            # SQLite için eski kod
            if pref_type == 'allergies':
                # Mevcut alerjileri sil
                conn.execute(text("DELETE FROM user_allergies WHERE user_id = :user_id"), 
                            {"user_id": user['id']})
                
                # Yeni alerjileri ekle
                for item in items:
                    # Alerji tablosunda var mı kontrol et
                    result = conn.execute(text("""
                        SELECT allergen_id FROM allergens WHERE name = :name
                    """), {"name": item})
                    
                    allergen_row = result.fetchone()
                    if allergen_row:
                        allergen_id = allergen_row[0]
                    else:
                        # Yeni alerji ekle
                        result = conn.execute(text("""
                            INSERT INTO allergens (name) VALUES (:name) RETURNING allergen_id
                        """), {"name": item})
                        allergen_id = result.fetchone()[0]
                    
                    # Kullanıcı alerjisini ekle
                    conn.execute(text("""
                        INSERT INTO user_allergies (user_id, allergen_id) VALUES (:user_id, :allergen_id)
                    """), {"user_id": user["id"], "allergen_id": allergen_id})
            
            elif pref_type == 'dislikes':
                # Mevcut sevilmeyenleri sil
                conn.execute(text("DELETE FROM user_dislikes WHERE user_id = :user_id"), 
                            {"user_id": user['id']})
                
                # Yeni sevilmeyenleri ekle
                for item in items:
                    # Ürün tablosunda var mı kontrol et
                    result = conn.execute(text("""
                        SELECT product_id FROM products WHERE canonical_name_en = :name
                    """), {"name": item})
                    
                    product_row = result.fetchone()
                    if product_row:
                        product_id = product_row[0]
                        # Kullanıcı sevilmeyenini ekle
                        conn.execute(text("""
                            INSERT INTO user_dislikes (user_id, product_id) VALUES (:user_id, :product_id)
                        """), {"user_id": user['id'], "product_id": product_id})
            
            elif pref_type == 'diet-preferences':
                # Mevcut diyet tercihlerini sil
                conn.execute(text("DELETE FROM user_dietary_preferences WHERE user_id = :user_id"), 
                            {"user_id": user['id']})
                
                # Yeni diyet tercihlerini ekle
                for item in items:
                    # Diyet tercihi tablosunda var mı kontrol et
                    result = conn.execute(text("""
                        SELECT pref_id FROM dietary_preferences WHERE label = :label
                    """), {"label": item})
                    
                    pref_row = result.fetchone()
                    if pref_row:
                        pref_id = pref_row[0]
                    else:
                        # Yeni diyet tercihi ekle
                        code = item.lower().replace(' ', '_')
                        result = conn.execute(text("""
                            INSERT INTO dietary_preferences (code, label) VALUES (:code, :label) RETURNING pref_id
                        """), {"code": code, "label": item})
                        pref_id = result.fetchone()[0]
                    
                    # Kullanıcı diyet tercihini ekle
                    conn.execute(text("""
                        INSERT INTO user_dietary_preferences (user_id, pref_id) VALUES (:user_id, :pref_id)
                    """), {"user_id": user['id'], "pref_id": pref_id})
            
            conn.commit()
            conn.close()
            
            return jsonify({'success': True, 'message': 'Tercihler başarıyla kaydedildi'})
        
    except Exception as e:
        logger.error(f"Tercih kaydetme hatası: {e}")
        return jsonify({'success': False, 'message': str(e)})

@app.route('/api/get-preferences', methods=['GET'])
@login_required
def get_preferences():
    """Kullanıcı tercihlerini getir"""
    try:
        user = get_current_user()
        conn = get_db_connection()
        cursor = conn.cursor()
        
        preferences = {
            'allergies': [],
            'dislikes': [],
            'diet_preferences': []
        }
        
        DATABASE_URL = os.environ.get('DATABASE_URL')
        if DATABASE_URL and DATABASE_URL.startswith('postgresql'):
            # PostgreSQL
            # Alerjileri getir
            cursor.execute("""
                SELECT a.name 
                FROM user_allergies ua
                JOIN allergens a ON ua.allergen_id = a.allergen_id
                WHERE ua.user_id = %s
            """, (user['user_id'],))
            preferences['allergies'] = [row[0] for row in cursor.fetchall()]
            
            # Sevilmeyenleri getir
            cursor.execute("""
                SELECT p.canonical_name_en 
                FROM user_dislikes ud
                JOIN products p ON ud.product_id = p.product_id
                WHERE ud.user_id = %s
            """, (user['user_id'],))
            preferences['dislikes'] = [row[0] for row in cursor.fetchall()]
            
            # Diyet tercihlerini getir
            cursor.execute("""
                SELECT dp.label 
                FROM user_dietary_preferences udp
                JOIN dietary_preferences dp ON udp.pref_id = dp.pref_id
                WHERE udp.user_id = %s
            """, (user['user_id'],))
            preferences['diet_preferences'] = [row[0] for row in cursor.fetchall()]
            
            cursor.close()
            conn.close()
        else:
            # SQLite için eski kod
            # Alerjileri getir
            result = conn.execute(text("""
                SELECT a.name 
                FROM user_allergies ua
                JOIN allergens a ON ua.allergen_id = a.allergen_id
                WHERE ua.user_id = :user_id
            """), {"user_id": user['id']})
            preferences['allergies'] = [row[0] for row in result]
            
            # Sevilmeyenleri getir
            result = conn.execute(text("""
            SELECT p.canonical_name_en 
            FROM user_dislikes ud
            JOIN products p ON ud.product_id = p.product_id
            WHERE ud.user_id = :user_id
        """), {"user_id": user['id']})
            preferences['dislikes'] = [row[0] for row in result]
            
            # Diyet tercihlerini getir
            result = conn.execute(text("""
                SELECT dp.label 
                FROM user_dietary_preferences udp
                JOIN dietary_preferences dp ON udp.pref_id = dp.pref_id
                WHERE udp.user_id = :user_id
            """), {"user_id": user['id']})
            preferences['diet_preferences'] = [row[0] for row in result]
            
            conn.close()
        
        return jsonify({'success': True, 'preferences': preferences})
        
    except Exception as e:
        logger.error(f"Tercih getirme hatası: {e}")
        return jsonify({'success': False, 'message': str(e)})

@app.route('/demo')
def demo():
    """Demo sayfası"""
    return render_template('demo.html')

if __name__ == '__main__':
    debug_mode = os.environ.get('DEBUG', 'False').lower() == 'true'
    host = os.environ.get('FLASK_HOST', '0.0.0.0')
    port = int(os.environ.get('FLASK_PORT', 5001))
    app.run(debug=debug_mode, host=host, port=port)
