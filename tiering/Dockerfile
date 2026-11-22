# The offload runner (tiering/README.md, "Offload runner"). Build context: tiering/.
FROM python:3.15-slim
WORKDIR /app
RUN pip install --no-cache-dir blake3
COPY offload ./offload
CMD ["python", "-u", "-m", "offload.runner"]
