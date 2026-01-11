import functools
import logging
import time
import pika
import os
import sys
from dotenv import load_dotenv
from consumers.news_parser import handle_news_message
from consumers.rss_atom_extractor import handle_rss_atom_message
from consumers.searcher import handle_searcher_message

LOG_FORMAT = ('%(levelname)-7s %(asctime)s %(name)-45s %(funcName)-35s %(lineno)-4d: %(message)s')
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
LOGGER = logging.getLogger(__name__)

# Load ENV
load_dotenv()

user = os.environ.get('RABBITMQ_DEFAULT_USER')
password = os.environ.get('RABBITMQ_DEFAULT_PASS')
host = os.environ.get('RABBITMQ_HOST')
port = os.environ.get('RABBITMQ_PORT')
news_queue_name = os.environ.get('NEWS_QUEUE_NAME')
rss_atom_queue_name = os.environ.get('RSS_ATOM_QUEUE_NAME')
searcher_queue_name = os.environ.get('SEARCHER_QUEUE_NAME')

if not all([user, password, host, port, news_queue_name, rss_atom_queue_name, searcher_queue_name]):
    LOGGER.error("Missing one or more required environment variables.")
    sys.exit(1)

try:
    port = int(port)
except ValueError:
    LOGGER.error("RABBITMQ_PORT must be a valid integer")
    sys.exit(1)

if len(sys.argv) != 2:
    LOGGER.error("Usage: python consumer.py <queue_name>")
    sys.exit(1)

queue_name = sys.argv[1]

QUEUE_HANDLERS = {
    news_queue_name: handle_news_message,
    rss_atom_queue_name: handle_rss_atom_message,
    searcher_queue_name: handle_searcher_message,
}

class Consumer:
    PREFETCH_COUNT = 1

    def __init__(self, amqp_url, queue_name):
        self.should_reconnect = False
        self.was_consuming = False

        self._connection = None
        self._channel = None
        self._closing = False
        self._consumer_tag = None
        self._amqp_url = amqp_url
        self._queue_name = queue_name
        self._consuming = False
        self._handler = QUEUE_HANDLERS.get(queue_name)

        if not self._handler:
            LOGGER.error(f"Unknown queue: {queue_name}")
            sys.exit(1)

    def connect(self):
        LOGGER.info(f'Connecting to RabbitMQ')
        return pika.SelectConnection(
            parameters=pika.URLParameters(self._amqp_url),
            on_open_callback=self.on_connection_open,
            on_open_error_callback=self.on_connection_open_error,
            on_close_callback=self.on_connection_closed)

    def close_connection(self):
        self._consuming = False
        if self._connection.is_closing or self._connection.is_closed:
            LOGGER.info('Connection is closing or already closed')
        else:
            LOGGER.info('Closing connection')
            self._connection.close()

    def on_connection_open(self, _unused_connection):
        LOGGER.info('Connection opened')
        self.open_channel()

    def on_connection_open_error(self, _unused_connection, err):
        LOGGER.error(f'Connection open failed: {err}')
        self.reconnect()

    def on_connection_closed(self, _unused_connection, reason):
        self._channel = None
        if self._closing:
            self._connection.ioloop.stop()
        else:
            LOGGER.warning(f'Connection closed, reconnect necessary: {reason}')
            self.reconnect()

    def reconnect(self):
        self.should_reconnect = True
        self.stop()

    def open_channel(self):
        LOGGER.info('Creating a new channel')
        self._connection.channel(on_open_callback=self.on_channel_open)

    def on_channel_open(self, channel):
        LOGGER.info('Channel opened')
        self._channel = channel
        self.add_on_channel_close_callback()
        self.setup_queue()

    def add_on_channel_close_callback(self):
        LOGGER.info('Adding channel close callback')
        self._channel.add_on_close_callback(self.on_channel_closed)

    def on_channel_closed(self, channel, reason):
        LOGGER.warning(f'Channel was closed: {reason}')
        self.close_connection()

    def setup_queue(self):
        LOGGER.info(f'Declaring queue {self._queue_name}')
        cb = functools.partial(self.on_queue_declareok, userdata=self._queue_name)
        self._channel.queue_declare(queue=self._queue_name, durable=True, callback=cb)

    def on_queue_declareok(self, _unused_frame, userdata):
        LOGGER.info(f'Queue declared: {userdata}')
        self.set_qos()

    def set_qos(self):
        self._channel.basic_qos(
            prefetch_count=self.PREFETCH_COUNT, callback=self.on_basic_qos_ok)

    def on_basic_qos_ok(self, _unused_frame):
        LOGGER.info(f'QOS set to: {self.PREFETCH_COUNT}')
        self.start_consuming()

    def start_consuming(self):
        LOGGER.info('Issuing consumer related RPC commands')
        self.add_on_cancel_callback()
        self._consumer_tag = self._channel.basic_consume(
            self._queue_name, self.on_message)
        self.was_consuming = True
        self._consuming = True

    def add_on_cancel_callback(self):
        LOGGER.info('Adding consumer cancellation callback')
        self._channel.add_on_cancel_callback(self.on_consumer_cancelled)

    def on_consumer_cancelled(self, method_frame):
        LOGGER.info(f'Consumer was cancelled remotely, shutting down: {method_frame}')
        self._channel.close()

    def on_message(self, _unused_channel, basic_deliver, properties, body):
        LOGGER.info(f'Received message # {basic_deliver.delivery_tag} from {properties.app_id}')
        try:
            # Ejecutamos el handler.
            # NOTA: Los handlers NO deben hacer ack/nack manual, solo procesar.
            self._handler(_unused_channel, basic_deliver, properties, body)
            
            # Si el handler termina sin errores, el Consumer centraliza el ACK aquí.
            LOGGER.info(f'Message processed successfully {basic_deliver.delivery_tag}, sending ACK')
            self._channel.basic_ack(basic_deliver.delivery_tag)
            
        except Exception as e:
            LOGGER.error(f'Error processing message {basic_deliver.delivery_tag}: {e}')
            # Si hubo un error, hacemos NACK para reencolar.
            self._channel.basic_nack(basic_deliver.delivery_tag, requeue=True)

    def acknowledge_message(self, delivery_tag):
        LOGGER.info(f'Acknowledging message {delivery_tag}')
        self._channel.basic_ack(delivery_tag)

    def stop_consuming(self):
        if self._channel:
            LOGGER.info('Sending a Basic.Cancel RPC command to RabbitMQ')
            cb = functools.partial(
                self.on_cancelok, userdata=self._consumer_tag)
            self._channel.basic_cancel(self._consumer_tag, cb)

    def on_cancelok(self, _unused_frame, userdata):
        self._consuming = False
        LOGGER.info(f'RabbitMQ acknowledged the cancellation of the consumer: {userdata}')
        self.close_channel()

    def close_channel(self):
        LOGGER.info('Closing the channel')
        self._channel.close()

    def run(self):
        self._connection = self.connect()
        self._connection.ioloop.start()

    def stop(self):
        if not self._closing:
            self._closing = True
            LOGGER.info('Stopping')
            if self._consuming:
                self.stop_consuming()
                self._connection.ioloop.start()
            else:
                self._connection.ioloop.stop()
            LOGGER.info('Stopped')

class ReconnectingConsumer:
    def __init__(self, amqp_url, queue_name):
        self._reconnect_delay = 0
        self._amqp_url = amqp_url
        self._queue_name = queue_name
        self._consumer = Consumer(self._amqp_url, self._queue_name)

    def run(self):
        while True:
            try:
                self._consumer.run()
            except KeyboardInterrupt:
                self._consumer.stop()
                break
            self._maybe_reconnect()

    def _maybe_reconnect(self):
        if self._consumer.should_reconnect:
            self._consumer.stop()
            reconnect_delay = self._get_reconnect_delay()
            LOGGER.info(f'Reconnecting after {reconnect_delay} seconds')
            time.sleep(reconnect_delay)
            self._consumer = Consumer(self._amqp_url, self._queue_name)

    def _get_reconnect_delay(self):
        if self._consumer.was_consuming:
            self._reconnect_delay = 0
        else:
            self._reconnect_delay += 1
        if self._reconnect_delay > 30:
            self._reconnect_delay = 30
        return self._reconnect_delay


def main():
    LOGGER.info(f"Starting consumer for queue: {queue_name}")
    amqp_url = f'amqp://{user}:{password}@{host}:{port}/%2F'
    consumer = ReconnectingConsumer(amqp_url, queue_name)
    consumer.run()


if __name__ == '__main__':
    main()