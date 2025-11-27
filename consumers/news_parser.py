import json
import logging
from newspaper import Article

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
        
        url = message.get('url')
        if not url:
            logger.error("No URL found in message")
            channel.basic_ack(delivery_tag=method.delivery_tag)
            return

        logger.info(f"Processing URL: {url}")
        
        article = Article(url)
        article.download()
        article.parse()
        
        logger.info(f"Successfully parsed article: {article.title}")
        # logger.info(f"Text: {article31.text[:200]}...")
        
        # TODO: Process the article content (save to DB, etc.)
        
        channel.basic_ack(delivery_tag=method.delivery_tag)
        
    except Exception as e:
        logger.error(f"Error processing message: {e}")
        # Acknowledge the message to prevent it from being requeued indefinitely in case of error
        # In a production system, you might want to dead-letter this
        channel.basic_ack(delivery_tag=method.delivery_tag)