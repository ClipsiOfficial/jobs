FROM python:3.14-slim AS consumer_base
WORKDIR /rabbitmq-consumers
RUN pip install --upgrade pip
COPY . .
RUN pip3 install -r requirements.txt
RUN python -c "import newspaper, os, shutil; \
    dest = os.path.join(os.path.dirname(newspaper.__file__), 'resources', 'text', 'stopwords-ca.txt'); \
    shutil.copy('/rabbitmq-consumers/stopwords-ca.txt', dest); \
    print(f'Stopwords copiados a: {dest}')"

FROM consumer_base AS news_base
CMD ["python", "consumer.py", "news"]

FROM consumer_base AS rss_atom_base
CMD ["python", "consumer.py", "rss_atom"]

FROM consumer_base AS searcher_base
CMD ["python", "consumer.py", "searcher"]

