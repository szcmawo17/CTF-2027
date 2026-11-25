FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --no-create-home appuser
COPY app.py ./
USER 10001:10001
EXPOSE 5000
CMD ["python", "app.py"]
