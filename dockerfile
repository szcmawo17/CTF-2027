FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir flask

RUN echo 'from flask import Flask' > app.py && \
    echo 'app = Flask(__name__)' >> app.py && \
    echo '@app.route("/")' >> app.py && \
    echo 'def home():' >> app.py && \
    echo '    return "<h1>CTF Test Service</h1><p>Docker is working!</p>"' >> app.py && \
    echo 'app.run(host="0.0.0.0", port=5000)' >> app.py

EXPOSE 5000

CMD ["python", "app.py"]