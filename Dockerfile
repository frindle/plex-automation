FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir requests flask
COPY arr-webhook.py .
COPY monthly_upgrade.py .
CMD ["python", "-u", "arr-webhook.py"]
