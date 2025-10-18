# AfiyetAI - Akıllı Mutfak Asistanı

AfiyetAI, fiş fotoğraflarını analiz ederek ürünleri tanıyan ve bu ürünlere göre kişiselleştirilmiş tarif önerileri sunan akıllı mutfak asistanıdır.

## Özellikler

- **Fiş Analizi**: Market fişlerini fotoğraflayarak ürünleri otomatik tanıma
- **Akıllı Envanter**: Satın alınan ürünleri otomatik olarak envantere ekleme
- **Tarif Önerileri**: Mevcut ürünlere göre kişiselleştirilmiş tarif önerileri
- **Raf Ömrü Takibi**: Ürünlerin son kullanma tarihlerini takip etme
- **Kullanıcı Paneli**: Kişisel hesap ve tercih yönetimi
- **Çoklu Dil Desteği**: Türkçe ve İngilizce arayüz

## Teknolojiler

- **Backend**: Flask (Python)
- **Veritabanı**: PostgreSQL / SQLite
- **OCR**: Tesseract
- **Görüntü İşleme**: OpenCV
- **API Entegrasyonu**: Spoonacular Recipe API
- **Frontend**: HTML, CSS, JavaScript
- **Deployment**: Docker

## Gereksinimler

- Python 3.8+
- PostgreSQL (opsiyonel)
- Tesseract OCR
- Docker (opsiyonel)

## Kurulum

### 1. Depoyu Klonlayın
```bash
git clone https://github.com/kullanici-adi/AfiyetAI.git
cd AfiyetAI
```

### 2. Sanal Ortam Oluşturun
```bash
python -m venv venv
source venv/bin/activate  # Linux/Mac
# veya
venv\Scripts\activate  # Windows
```

### 3. Bağımlılıkları Yükleyin
```bash
pip install -r requirements.txt
```

### 4. Ortam Değişkenlerini Ayarlayın
```bash
cp env.example .env
# .env dosyasını düzenleyin
```

### 5. Veritabanını Başlatın
```bash
python app.py
```

## Docker ile Çalıştırma

```bash
docker-compose up -d
```

## Kullanım

1. **Kayıt Olun**: Yeni hesap oluşturun
2. **Fiş Yükleyin**: Market fişinizin fotoğrafını yükleyin
3. **Ürünleri Görün**: Tanınan ürünleri kontrol edin
4. **Tarif Alın**: Önerilen tarifleri inceleyin
5. **Envanteri Yönetin**: Ürünlerinizi takip edin

## API Endpoints

- `POST /upload` - Fiş yükleme
- `GET /api/inventory` - Envanter listesi
- `POST /api/process` - Fiş işleme (JSON)
- `POST /api/save-preferences` - Kullanıcı tercihleri

POST /api/process
{
  "success": true,
  "products": [
    {"name": "Süt", "normalized_text_en": "Milk", "price": "25.90"}
  ]
}


## Proje Yapısı

```
AfiyetAI/
├── app.py                 # Ana Flask uygulaması
├── receipt_ingest.py     # Fiş analiz modülü
├── recipe_recommender.py # Tarif öneri sistemi
├── translate_utils.py    # Çeviri yardımcıları
├── static/               # CSS, JS, resimler
├── templates/            # HTML şablonları
├── uploads/              # Yüklenen dosyalar
└── requirements.txt      # Python bağımlılıkları
```

## Güvenlik

- Şifre hash'leme (Werkzeug)
- Dosya güvenlik kontrolleri
- Rate limiting
- Session yönetimi
- SQL injection koruması

## Çevre Değişkenleri

```env
SECRET_KEY=your-secret-key
DATABASE_URL=postgresql://user:pass@localhost/db
FLASK_ENV=production
MAX_UPLOADS_PER_HOUR=10
```

## Lisans

Bu proje MIT lisansı altında lisanslanmıştır.

## Katkıda Bulunma

1. Fork yapın
2. Feature branch oluşturun (`git checkout -b feature/AmazingFeature`)
3. Commit yapın (`git commit -m 'Add some AmazingFeature'`)
4. Push yapın (`git push origin feature/AmazingFeature`)
5. Pull Request oluşturun

## İletişim

Proje hakkında sorularınız için issue açabilir veya iletişime geçebilirsiniz.

---

**AfiyetAI** - Mutfağınızı akıllı hale getirin! 
