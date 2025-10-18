import os  
from dotenv import load_dotenv  
  
load_dotenv()  
  
# API Configuration  
SPOONACULAR_API_KEY = os.getenv("SPOONACULAR_API_KEY")  
  
# Application Settings  
DEBUG = os.getenv("DEBUG", "False").lower() == "true"  
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")  
  
# File Paths  
SHELF_LIFE_CSV_PATH = os.getenv("SHELF_LIFE_CSV_PATH", "data/raf_omru.csv")  
PRODUCT_DICTIONARY_PATH = os.getenv("PRODUCT_DICTIONARY_PATH", "data/product_dictionary.json")  
TURKISH_ENGLISH_TRANSLATION_PATH = os.getenv("TURKISH_ENGLISH_TRANSLATION_PATH", "data/turkish_english_translation.json") 
