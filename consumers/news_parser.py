import json
import logging
import newspaper
from google import genai
import os
import requests
import tldextract
from urllib.parse import urlparse
import wordninja_enhanced as wordninja

logger = logging.getLogger(__name__)

def extract_source_name(url: str) -> str:
    """Extract and format a readable source name from a URL.
    
    Examples:
        'https://www.elpais.com/article' -> 'El Pais'
        'https://lavanguardia.com/news' -> 'La Vanguardia'
        'https://www.bbc.co.uk/news' -> 'Bbc'
    
    :param url: The URL to extract the source from
    :return: A formatted source name
    """
    try:
        extracted = tldextract.extract(url)
        main_name = extracted.domain
        suffix = extracted.suffix
        
        # Map TLD to language for better accuracy
        tld_to_lang = {
            'es': 'es',
            'cat': 'es',  # Catalan domains, use Spanish as closest
            'fr': 'fr',
            'de': 'de',
            'it': 'it',
            'pt': 'pt',
        }
        
        # Try to detect language from TLD, otherwise use multiple attempts
        tld_end = suffix.split('.')[-1] if suffix else ''
        detected_lang = tld_to_lang.get(tld_end)
        
        best_words = []
        min_parts = float('inf')
        
        # Try different language models and pick the one with fewer parts (more likely correct)
        languages_to_try = [detected_lang] if detected_lang else ['es', 'en', 'fr', 'de', 'it', 'pt']
        
        for lang in languages_to_try:
            try:
                lm = wordninja.LanguageModel(language=lang)
                words = lm.split(main_name)
                # Prefer splits with fewer parts (more likely to be correct)
                if len(words) < min_parts:
                    min_parts = len(words)
                    best_words = words
            except Exception:
                continue
        
        # If no language model worked, use default
        if not best_words:
            best_words = wordninja.split(main_name)
        
        # Capitalize each word
        formatted = ' '.join(word.capitalize() for word in best_words)
        
        return formatted if formatted else "Unknown"
    except Exception as e:
        logger.warning(f"Failed to extract source from URL {url}: {e}")
        return "Unknown"

def handle_news_message(channel, method, properties, body):
    """Handle incoming news messages.
    
    :param pika.channel.Channel channel: The channel object
    :param pika.Spec.Basic.Deliver method: The delivery method
    :param pika.Spec.BasicProperties properties: Message properties
    :param bytes body: The message body
    """
    try:
        message = json.loads(body)
        logger.info(f"Received message: {message}")

        # Extract relevant fields from the message
        keyword_id = message.get('keyword_id')
        rss_atom_id = message.get('rss_atom_id')
        url = message.get('url')
        if not url:
            logger.error("No URL found in message")
            return
        
        # Check if the URL is already processed (call backend API)
        api_url = os.environ.get('API_URL', 'http://localhost:8787')
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f"Bearer {os.environ.get('API_TOKEN', '')}"
        }
        responseBackend = requests.get(
            f"{api_url}/admin/news/exists",
            headers=headers,
            params={'url': url}
        )
        if responseBackend.status_code != 200:
            raise Exception(f"Failed to check existing articles: {responseBackend.text}")

        backend_json = responseBackend.json()
        if isinstance(backend_json, dict) and backend_json.get('exists', False):
            logger.info(f"URL already processed: {url}")
            return

        # Initialize Newspaper3 and Gemini GenAI client
        geminiClient = genai.Client()
        config = newspaper.Config()
        config.browser_user_agent = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.3'
        config.fetch_images = False
        
        # Start processing the article
        logger.info(f"Processing URL: {url}")
        article = newspaper.Article(url, config=config)
        article.download()
        article.parse()
        logger.info(f"Successfully parsed article: {article.title}")

        # Send to Gemini Gemma via API for summarization
        prompt = f"ROLE: You are a strict AI script specialized in news headlines. \
                   INSTRUCTIONS: Read the following news article. Your ONLY task is to generate a concise summary. \
                   LANGUAGE REQUIREMENT: Detect the language of the provided text. The output MUST be in the EXACT SAME language as the input text. \
                   FORMAT: Maximum 100 characters. Strictly NO emojis, NO unnecessary symbols, and NO introductory text or explanations. \
                   STRICT OUTPUT: Provide ONLY the summary text. \
                   INPUT TITLE (CONTEXT): {article.title} \
                   INPUT TEXT: {article.text}"
        responseGemini = geminiClient.models.generate_content(
            model="gemma-3-27b-it",
            contents=prompt
        )
        logger.info(f"Summary from Gemini Gemma: {responseGemini.text[:50]}")

        # Create payload to send back to backend API
        payload = {
            'keyword_id': keyword_id,
            'url': url,
            'title': article.title,
            'summary': responseGemini.text,
            'source': extract_source_name(url),
        }

        # Only include numeric rss_atom_id if provided
        if rss_atom_id not in (None, ''):
            try:
                payload['rss_atom_id'] = int(rss_atom_id)
            except (TypeError, ValueError):
                logger.warning(f"Invalid rss_atom_id value: {rss_atom_id}, skipping this field")

        # Only include published_date when available
        if article.publish_date:
            try:
                payload['published_date'] = article.publish_date.isoformat()
            except Exception as e:
                logger.warning(f"Could not serialize publish_date: {e}")

        responseSave = requests.post(
            f"{api_url}/admin/news",
            headers=headers,
            json=payload
        )
        if responseSave.status_code != 201:
            raise Exception(f"Failed to save article: {responseSave.text}")

        logger.info(f"Successfully saved article for URL: {url}")
        
    except json.JSONDecodeError as e:
        logger.error(f"JSON Decode Error, discarding message: {e}")
        # Discard message (return -> ack in consumer)
        return
    except Exception as e:
        logger.error(f"Error processing message: {e}")
        # Re-raise to trigger NACK in consumer
        raise e