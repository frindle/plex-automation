FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY arr-webhook.py .
COPY monthly_upgrade.py .
COPY media_share.py .
CMD ["python", "-u", "arr-webhook.py"]
