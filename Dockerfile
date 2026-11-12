FROM python:3.14-slim
WORKDIR /app
# unar: unpacks library RAR sets (rar_extract.py); Plex can't play .rar
RUN apt-get update && apt-get install -y --no-install-recommends unar && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY arr-webhook.py .
COPY monthly_upgrade.py .
COPY media_share.py .
COPY llm_decide.py .
COPY llm_advisor.py .
COPY library_audit.py .
COPY downloads_audit.py .
COPY episode_retention.py .
COPY rar_extract.py .
COPY torrent_breakdown.py .
COPY tiering_gate.py .
COPY speed_cap.py .
CMD ["python", "-u", "arr-webhook.py"]
