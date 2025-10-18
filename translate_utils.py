# translate_utils.py
import os
import requests
import logging
from dotenv import load_dotenv

# .env dosyasını yükle
load_dotenv()

logger = logging.getLogger(__name__)
GOOGLE_TRANSLATE_API_KEY = os.getenv("GOOGLE_TRANSLATE_API_KEY") or None

def translate_text(text: str, source_lang: str = "en", target_lang: str = "tr") -> str:
    """Sadece Google Translate API kullanır"""
    if not text or not GOOGLE_TRANSLATE_API_KEY:
        logger.warning(f"Missing text or API key: text='{text}', key={GOOGLE_TRANSLATE_API_KEY[:20] if GOOGLE_TRANSLATE_API_KEY else None}")
        return text
    try:
        url = "https://translation.googleapis.com/language/translate/v2"
        params = {
            "q": text,
            "source": source_lang,
            "target": target_lang,
            "format": "text",
            "key": GOOGLE_TRANSLATE_API_KEY,
        }
        r = requests.post(url, data=params, timeout=6)
        r.raise_for_status()
        data = r.json()
        translated = data["data"]["translations"][0]["translatedText"]
        logger.info(f"Translation successful: '{text}' -> '{translated}'")
        return translated
    except Exception as e:
        logger.warning(f"Translate fail '{text}': {e}")
        return text  # fallback
