FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MCP_HOST=0.0.0.0 \
    ARTIFACT_HOST=0.0.0.0
RUN apt-get update && apt-get install -y --no-install-recommends \
    libcairo2 libffi8 libgdk-pixbuf-2.0-0 libpango-1.0-0 libpangoft2-1.0-0 \
    libharfbuzz0b libharfbuzz-subset0 shared-mime-info fonts-liberation \
    libreoffice-writer-nogui \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py docuseal_mcp.py document_export.py document_worker.py artifact_server.py ./
RUN mkdir -p /var/lib/docuseal-artifacts && chown 10001:10001 /var/lib/docuseal-artifacts
USER 10001:10001
EXPOSE 8000 8001
CMD ["python", "server.py"]
