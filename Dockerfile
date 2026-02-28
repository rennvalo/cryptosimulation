# ---------------------------------------------------------------------------
# Dockerfile — Single image used by both the node and miner services.
#
# The CMD is NOT set here; docker-compose.yml overrides it per service:
#   node    → uvicorn node:app  --host 0.0.0.0 --port 8182
#   miner_N → uvicorn miner:app --host 0.0.0.0 --port 818N
#
# Build: docker compose up --build
# ---------------------------------------------------------------------------

FROM python:3.11-slim

# Set working directory inside the container
WORKDIR /app

# Copy and install dependencies first so Docker can cache this layer.
# Re-installing only happens when requirements.txt changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all application source files
COPY block.py blockchain.py node.py miner.py transaction.py wallet.py ./

# Expose the default port (overridden per service in docker-compose.yml)
EXPOSE 8182
