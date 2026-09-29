FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MCP_HOST=0.0.0.0
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py docuseal_api.py ./
USER 10001:10001
EXPOSE 8000
CMD ["python", "server.py"]
