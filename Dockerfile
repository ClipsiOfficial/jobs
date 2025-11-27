FROM python:3.14-slim AS consumer_base
WORKDIR /rabbitmq-consumers
RUN pip install --upgrade pip
COPY . .
RUN pip3 install -r requirements.txt

FROM consumer_base AS news_base
CMD ["python", "consumer.py", "news"]

FROM consumer_base AS rss_atom_base
CMD ["python", "consumer.py", "rss_atom"]

FROM consumer_base AS searcher_base
CMD ["python", "consumer.py", "searcher"]

FROM ollama/ollama:latest AS ollama_base

RUN ollama serve & sleep 5 && ollama pull fabriciocarraro/BSC-LT-salamandra-7B-instruct-gguf:latest