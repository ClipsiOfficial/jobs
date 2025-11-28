import json
import logging
import newspaper
from google import genai
import os
import requests

logger = logging.getLogger(__name__)

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
            channel.basic_ack(delivery_tag=method.delivery_tag)
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
            logger.error(f"Failed to check existing articles: {responseBackend.text}")
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return
        if responseBackend.json().exists:
            logger.info(f"URL already processed: {url}")
            channel.basic_ack(delivery_tag=method.delivery_tag)
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
            'rss_atom_id': rss_atom_id,
            'url': url,
            'title': article.title,
            'summary': responseGemini.text,
            'published_date': article.publish_date.isoformat() if article.publish_date else None,
        }
        responseSave = requests.post(
            f"{api_url}/admin/news",
            headers=headers,
            json=payload
        )
        if responseSave.status_code != 201:
            logger.error(f"Failed to save article: {responseSave.text}")
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return
        logger.info(f"Successfully saved article for URL: {url}")

        channel.basic_ack(delivery_tag=method.delivery_tag)
        
    except Exception as e:
        logger.error(f"Error processing message: {e}")
        # Acknowledge the message to prevent it from being requeued indefinitely in case of error
        # In a production system, you might want to dead-letter this
        channel.basic_ack(delivery_tag=method.delivery_tag)