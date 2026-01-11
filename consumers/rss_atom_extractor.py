import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import feedparser
import newspaper
import pika
import requests

logger = logging.getLogger(__name__)

# Threshold for processing old news (in hours)
NEWS_MAX_AGE_HOURS = 1


class RssAtomExtractor:
    """Extracts news from RSS/Atom feeds and matches them with keywords."""
    
    def __init__(self):
        self.news_queue_name = os.environ.get('NEWS_QUEUE_NAME', 'news')
        self._newspaper_config = self._create_newspaper_config()
    
    def _create_newspaper_config(self) -> newspaper.Config:
        """Create and return a configured newspaper Config object."""
        config = newspaper.Config()
        config.browser_user_agent = (
            'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.3'
        )
        config.fetch_images = False
        config.request_timeout = 15
        return config
    
    def fetch_feed(self, feed_url: str) -> Optional[feedparser.FeedParserDict]:
        """Fetch and parse an RSS/Atom feed.
        
        :param feed_url: URL of the RSS/Atom feed
        :return: Parsed feed or None if failed
        """
        try:
            feed = feedparser.parse(feed_url)
            
            if feed.bozo and feed.bozo_exception:
                logger.warning(f"Feed parsing warning for {feed_url}: {feed.bozo_exception}")
            
            if not feed.entries:
                logger.info(f"No entries found in feed: {feed_url}")
                return None
                
            logger.info(f"Successfully fetched {len(feed.entries)} entries from {feed_url}")
            return feed
            
        except Exception as e:
            logger.error(f"Failed to fetch feed {feed_url}: {e}")
            return None
    
    def download_article_text(self, url: str) -> Optional[str]:
        """Download and extract text content from an article URL.
        
        :param url: URL of the article
        :return: Article text or None if failed
        """
        try:
            article = newspaper.Article(url, config=self._newspaper_config)
            article.download()
            article.parse()
            
            # Combine title and text for keyword matching
            full_text = f"{article.title or ''} {article.text or ''}"
            return full_text.lower() if full_text.strip() else None
            
        except Exception as e:
            logger.warning(f"Failed to download article {url}: {e}")
            return None
    
    def find_matching_keywords(
        self, 
        text: str, 
        keywords: dict[str, int]
    ) -> Optional[tuple[int, int]]:
        """Find the most frequently matching keyword in the text.
        
        :param text: The article text (lowercase)
        :param keywords: Dict mapping keyword content to keyword ID
        :return: Tuple of (keyword_id, count) for the best match, or None if no match
        """
        best_keyword_id = None
        max_count = 0
        
        for keyword, keyword_id in keywords.items():
            # Use word boundary regex for accurate matching
            pattern = r'\b' + re.escape(keyword.lower()) + r'\b'
            matches = re.findall(pattern, text)
            count = len(matches)
            
            if count > max_count:
                max_count = count
                best_keyword_id = keyword_id
        
        return (best_keyword_id, max_count) if best_keyword_id else None
    
    def extract_entry_url(self, entry: feedparser.FeedParserDict) -> Optional[str]:
        """Extract the URL from a feed entry.
        
        :param entry: A feed entry
        :return: URL string or None
        """
        # Try different common fields for the URL
        url = entry.get('link') or entry.get('url') or entry.get('id')
        
        if url and url.startswith('http'):
            return url
        return None
    
    def is_recent_news(self, entry: feedparser.FeedParserDict, max_age_hours: int) -> bool:
        """Check if the news entry is recent enough to process.
        
        :param entry: A feed entry
        :param max_age_hours: Maximum age in hours to consider recent
        :return: True if the entry is recent enough, False otherwise
        """
        try:
            # Try to extract published date from entry
            published_struct = entry.get('published_parsed') or entry.get('updated_parsed')
            
            if not published_struct:
                logger.warning(f"No date found in entry: {entry.get('title', 'Unknown')}")
                return False
            
            # Convert struct_time to datetime
            published_dt = datetime.fromtimestamp(
                __import__('time').mktime(published_struct),
                tz=timezone.utc
            )
            
            # Compare with current time
            now = datetime.now(timezone.utc)
            age = now - published_dt
            max_age = timedelta(hours=max_age_hours)
            
            is_recent = age <= max_age
            
            if is_recent:
                logger.info(f"Article is recent ({age.total_seconds()/3600:.1f}h old): {entry.get('title', 'Unknown')}")
            else:
                logger.info(f"Article is too old ({age.total_seconds()/3600:.1f}h): skipping {entry.get('title', 'Unknown')}")
            
            return is_recent
            
        except Exception as e:
            logger.warning(f"Failed to check entry date: {e}, processing anyway")
            # Process if we can't determine the date
            return True
    
    def publish_to_news_queue(
        self,
        channel: pika.channel.Channel,
        url: str,
        keyword_id: int,
        rss_atom_id: int
    ) -> bool:
        """Publish a news item to the news processing queue.
        
        :param channel: RabbitMQ channel
        :param url: URL of the news article
        :param keyword_id: ID of the matched keyword
        :param rss_atom_id: ID of the RSS/Atom source
        :return: True if successful, False otherwise
        """
        try:
            news_item = {
                'url': url,
                'keyword_id': keyword_id,
                'rss_atom_id': rss_atom_id,
            }
            
            channel.basic_publish(
                exchange='',
                routing_key=self.news_queue_name,
                body=json.dumps(news_item),
                properties=pika.BasicProperties(
                    delivery_mode=2,  # Persistent message
                    content_type='application/json',
                )
            )
            
            logger.debug(f"Published news item to queue: {url} (keyword_id={keyword_id})")
            return True
            
        except Exception as e:
            logger.error(f"Failed to publish news item {url}: {e}")
            return False


def handle_rss_atom_message(channel, method, properties, body):
    """Handle incoming RSS/Atom feed messages.
    
    Expected message format:
    {
        "rss_atom_id": int,
        "feed_url": str,
        "keywords": {"keyword_content": keyword_id, ...}
    }
    
    :param pika.channel.Channel channel: The channel object
    :param pika.Spec.Basic.Deliver method: The delivery method
    :param pika.Spec.BasicProperties properties: Message properties
    :param bytes body: The message body
    """
    extractor = RssAtomExtractor()
    
    try:
        # Parse incoming message
        message = json.loads(body)
        logger.info(f"Processing RSS/Atom message: rss_atom_id={message.get('rss_atom_id')}")
        
        # Validate required fields
        rss_atom_id = message.get('rss_atom_id')
        feed_url = message.get('feed_url')
        keywords = message.get('keywords', {})
        
        if not rss_atom_id:
            logger.error("Missing rss_atom_id in message")
            return
            
        if not feed_url:
            logger.error(f"Missing feed_url in message for rss_atom_id={rss_atom_id}")
            return
            
        if not keywords:
            logger.warning(f"No keywords provided for feed {feed_url}, skipping")
            return
        
        # Fetch and parse the feed
        feed = extractor.fetch_feed(feed_url)
        if not feed:
            logger.warning(f"Could not fetch feed: {feed_url}")
            return
        
        # Process each entry in the feed
        published_count = 0
        processed_count = 0
        skipped_old_count = 0
        
        for entry in feed.entries:
            processed_count += 1
            
            # Check if the news is recent enough (less than 1 hour old)
            if not extractor.is_recent_news(entry, NEWS_MAX_AGE_HOURS):
                skipped_old_count += 1
                continue
            
            # Extract URL from entry
            entry_url = extractor.extract_entry_url(entry)
            if not entry_url:
                logger.debug(f"Skipping entry without valid URL")
                continue
            
            # Download article content for keyword matching
            article_text = extractor.download_article_text(entry_url)
            if not article_text:
                logger.debug(f"Could not download article text for: {entry_url}")
                continue
            
            # Find the most matching keyword
            best_match = extractor.find_matching_keywords(article_text, keywords)
            
            if not best_match:
                logger.debug(f"No keywords matched for: {entry_url}")
                continue
            
            best_keyword_id, match_count = best_match
            logger.info(f"Best keyword match for {entry_url}: keyword_id={best_keyword_id} (count={match_count})")
            
            # Publish to news queue with the best matching keyword
            success = extractor.publish_to_news_queue(
                channel=channel,
                url=entry_url,
                keyword_id=best_keyword_id,
                rss_atom_id=rss_atom_id,
            )
            if success:
                published_count += 1
        
        logger.info(
            f"Finished processing feed {feed_url}: "
            f"{processed_count} entries processed, {published_count} news items published, {skipped_old_count} skipped (too old)"
        )
        
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in message: {e}")
        # Return to ACK and discard
        return
        
    except Exception as e:
        logger.error(f"Error processing RSS/Atom message: {e}", exc_info=True)
        # Re-raise to NACK and retry
        raise e