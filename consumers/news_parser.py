import json
import logging
import newspaper
from google import genai
import os
import requests
import tldextract
from urllib.parse import urlparse
import wordninja_enhanced as wordninja
import re
from lxml.html import fromstring

logger = logging.getLogger(__name__)

# Regex for common catalog/navigation URL patterns
CATALOG_PATTERNS = re.compile(
    r'(/tag/|/tags/|/category/|/categoria/|/page/|/pagina/|/search/|/busqueda/|/author/|/autor/|/archive/|/hemeroteca/|/topic/|/temas/)',
    re.IGNORECASE
)

def get_link_density(html_content: str) -> float:
    """Calculate the ratio of text inside <a> tags vs total text."""
    if not html_content:
        return 0.0
    try:
        doc = fromstring(html_content)
        text_content = doc.text_content().strip()
        if not text_content:
            return 0.0
        
        # Calculate text length inside <a> tags
        link_text = "".join([a.text_content() for a in doc.xpath('//a')])
        
        return len(link_text) / len(text_content)
    except Exception:
        return 0.0

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
        # Robust decoding logic for uncertain encoding (UTF-8 vs Latin-1)
        if isinstance(body, bytes):
            try:
                decoded_body = body.decode('utf-8')
            except UnicodeDecodeError:
                logger.warning(f"UTF-8 decode failed. Falling back to Latin-1.")
                try:
                    decoded_body = body.decode('latin-1')
                except UnicodeDecodeError:
                    decoded_body = body.decode('utf-8', errors='replace')
        else:
            decoded_body = body

        message = json.loads(decoded_body)
        logger.info(f"Received message: {message}")

        # Extract relevant fields from the message
        keyword_id = message.get('keyword_id')
        rss_atom_id = message.get('rss_atom_id')
        url = message.get('url')
        if not url:
            logger.error("No URL found in message")
            return
        
        # Check URL patterns
        if CATALOG_PATTERNS.search(url):
            logger.info(f"Filtered (URL Pattern): {url} matches catalog regex.")
            return

        # Check Root Path (Homepage)
        parsed_u = urlparse(url)
        if parsed_u.path.strip("/") == "":
            logger.info(f"Filtered (Root Path): {url} is a homepage.")
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
        
        # Check Link Density (Catalog Detection)
        link_density = get_link_density(article.html)
        if link_density > 0.65:  # Threshold: >65% of text is links
            logger.info(f"Filtered (Link Density): {url} has high link density ({link_density:.2f}).")
            return

        article.parse()
        logger.info(f"Successfully parsed article: {article.title}")

        # Check Title Length (Short titles are often categories/homes)
        if len(article.title.split()) < 4:
            logger.info(f"Filtered (Short Title): {url} title '{article.title}' is too short.")
            return

        # Check text length (Short Content)
        if len(article.text) < 250:
             logger.info(f"Filtered (Short Content): {url} text length is {len(article.text)} chars.")
             return

        # Check Metadata (og:type)
        og_type = article.meta_data.get('og', {}).get('type', '').lower()
        if og_type in ['website', 'archive', 'blog', 'category']:
            logger.info(f"Filtered (Metadata): {url} has og:type='{og_type}'.")
            return

        # Send to Gemini Gemma via API for summarization
        prompt = f"ROLE: You are an expert AI news analyzer. \
                   INSTRUCTIONS: Read the provided text. Determine if this is a VALID single news article. \
                   If is NOT a valid article, reject it IMMEDIATELY by responding with 'INVALID_CONTENT'. \
                   REJECT CRITERIA: \
                   - A list of headlines or links. \
                   - A category page, menu, or navigation. \
                   - A cookie consent notice or privacy policy. \
                   - A subscription request. \
                   - An error message (e.g. 'Please provide text'). \
                   - A general description of a topic without a specific event. \
                   VALID ARTICLE OUTPUT FORMAT: If it is a valid article, generate a concise summary in the detected language of the article! (max 100 chars, no emojis). \
                   Do not add any prefix, suffix, or quotes. It must be only the summary text. \
                   INPUT TITLE: {article.title} \
                   INPUT TEXT: {article.text}"
        
        responseGemini = geminiClient.models.generate_content(
            model="gemma-3-27b-it",
            contents=prompt
        )
        logger.info(f"Summary from Gemini Gemma: {responseGemini.text[:50]}")

        if 'INVALID_CONTENT' in responseGemini.text:
            logger.info(f"Filtered (AI Validation): {url} flagged as non-article by Gemini.")
            return

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