import json
import logging
import os
import pika
from serpapi import GoogleSearch
import requests

LOGGER = logging.getLogger(__name__)

non_news_sources = [
    # --- Video y Streaming ---
    "youtube.com", 
    "youtu.be", 
    "twitch.tv", 
    "vimeo.com", 
    "dailymotion.com",
    "tiktok.com", 
    "netflix.com", 
    "spotify.com",
    "soundcloud.com",
    "hulu.com",

    # --- Enciclopedias y Referencia ---
    "wikipedia.org", 
    "wikimedia.org", 
    "wikihow.com", 
    "fandom.com", 
    "imdb.com", 
    "genius.com",       # Letras de canciones
    "dictionary.com", 
    "britannica.com", 
    "archive.org",
    "rotentomatoes.com",
    "somsardana.com",

    # --- Redes Sociales y Foros ---
    "facebook.com", 
    "instagram.com", 
    "twitter.com", 
    "x.com", 
    "linkedin.com", 
    "pinterest.com", 
    "reddit.com",       # Ojo: a veces tiene "noticias", pero es un foro
    "tumblr.com", 
    "discord.com",
    "t.me",             # Telegram links
    "snapchat.com",
    "quora.com",        # Preguntas y respuestas
    "nextdoor.com",
    "vk.com",

    # --- E-commerce (Tiendas) ---
    "amazon.com", 
    "amazon.es",        # Variantes regionales
    "ebay.com", 
    "aliexpress.com", 
    "temu.com",
    "etsy.com", 
    "walmart.com", 
    "mercadolibre.com",
    "shein.com",
    "bestbuy.com",
    "target.com",
    "craigslist.org",
    
    # --- Turismo y Reseñas ---
    "tripadvisor.com",
    "booking.com",
    "airbnb.com",
    "yelp.com",
    "expedia.com",

    # --- Tech, Repositorios y Corporativos ---
    "github.com", 
    "gitlab.com", 
    "stackoverflow.com", 
    "microsoft.com", 
    "google.com",       # La home de Google
    "apple.com", 
    "adobe.com",
    "sourceforge.net",
    "wordpress.org",
    "wordpress.com",    # La home, no los blogs alojados

    # --- Buscadores y Portales Genéricos ---
    "bing.com", 
    "yahoo.com", 
    "msn.com", 
    "aol.com",
    "duckduckgo.com",
    "weather.com",
    "news.google.com",
    "live.com",
    "excite.com",
    "zapmeta.com",
    "webcrawler.com",
    "dogpile.com",
    "infoseek.com",
    "lycos.com",

    # --- Otros ---
    "slideshare.net",
    "scribd.com",
    "behance.net",
    "deviantart.com",
    "patreon.com",
    "kickstarter.com",
    "gofundme.com",
    "eventbrite.com",
    "meetup.com",
    "9gag.com",
    "ifttt.com",
]

def handle_searcher_message(channel, method, properties, body):
    """Handle incoming searcher messages.
    
    :param pika.channel.Channel channel: The channel object
    :param pika.Spec.Basic.Deliver method: The delivery method
    :param pika.Spec.BasicProperties properties: Message properties
    :param bytes body: The message body
    """
    try:
        message = json.loads(body)
        project_id = message.get('project_id')
        topic = message.get('topic')
        keyword_id = message.get('keyword_id')
        keyword = message.get('keyword')
        searches = message.get('searches', 0)
        query = 'Noticies de ' + keyword + ' ' + topic
        
        if not topic or not keyword:
            LOGGER.error("No topic or keyword found in message")
            channel.basic_ack(delivery_tag=method.delivery_tag, requeue=False)
            return

        LOGGER.info(f"Searching for: {query}")

        api_key = os.environ.get('SERP_API_KEY')
        if not api_key:
            LOGGER.error("SERP_API_KEY not set in environment variables")
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return
        
        params = {
            "engine": "google_light",
            "q": query,
            "location": "Catalonia,Spain",
            "gl": "es",
            "hl": "ca",
            "start": searches * 10,
            "json_restrictor": "organic_results",
            "api_key": api_key,
        }

        search = GoogleSearch(params)
        results = search.get_dict()
        news_results = results.get("organic_results", [])
        
        if not results:
            LOGGER.error("No results returned from SerpApi")
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return
        if results.get("error"):
            LOGGER.error(f"Errors returned from SerpApi: {results.get('error')}")
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return
        if not news_results:
            LOGGER.info("No news results found, setting keyword as fully processed")
            set_keyword_fully_processed(keyword_id)
            channel.basic_ack(delivery_tag=method.delivery_tag)
            return

        process_search_results(channel, news_results, project_id, keyword_id)
        channel.basic_ack(delivery_tag=method.delivery_tag)

    except Exception as e:
        LOGGER.error(f"Error processing message: {e}")
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

def set_keyword_fully_processed(keyword_id):
    """Set the keyword as fully processed in the database by calling the appropriate API endpoint"""

    api_url = os.environ.get('API_URL')
    if(not api_url):
        LOGGER.error("API_URL not set, cannot set keyword as fully processed")
        return
    api_token = os.environ.get('API_TOKEN')
    if(not api_token):
        LOGGER.error("API_TOKEN not set, cannot set keyword as fully processed")
        return
    endpoint = f"{api_url}/admin/keyword/{keyword_id}/processed"
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json"
    }
    try:
        response = requests.post(endpoint, headers=headers)
        if response.status_code == 200:
            LOGGER.info(f"Keyword {keyword_id} set as fully processed.")
        else:
            LOGGER.error(f"Failed to set keyword {keyword_id} as fully processed. Status code: {response.status_code}, Response: {response.text}")
    except Exception as e:
        LOGGER.error(f"Error setting keyword {keyword_id} as fully processed: {e}")
    return

def process_search_results(channel, news_results, project_id, keyworkd_id):
    """Process and publish search results to the news queue"""
    news_queue_name = os.environ.get('NEWS_QUEUE_NAME', 'news')
    for result in news_results:
        url = result.get("link")
        if not url or not url.startswith("http"):
            LOGGER.warning(f"Skipping invalid URL: {url}")
            continue

        if any(source in url for source in non_news_sources):
            LOGGER.warning(f"Skipping non-news source URL: {url}")
            continue

        # Construct the message for the news queue
        news_item = {
            "project_id": project_id,
            "keyword_id": keyworkd_id,
            "url": result.get("link"),
            "title": result.get("title", ""),
            "date": result.get("date", ""),
        }
        
        channel.basic_publish(
            exchange='',
            routing_key=news_queue_name,
            body=json.dumps(news_item),
            properties=pika.BasicProperties(
                delivery_mode=2,  # make message persistent
            ))
    LOGGER.info(f"Published {len(news_results)} news items to {news_queue_name}")