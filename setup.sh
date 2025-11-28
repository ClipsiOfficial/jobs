source .venv/bin/activate
pip3 install -r requirements.txt
cp stopwords-ca.txt .venv/lib64/python3.13/site-packages/newspaper/resources/text/
cp stopwords-ca.txt .venv/lib/python3.13/site-packages/newspaper/resources/text/