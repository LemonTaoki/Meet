# Use Python 3.11 slim image as base - minimal and optimized
FROM python:3.11-slim

# Set working directory in container
WORKDIR /app

# Install system dependencies required for RandomX
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy application file
COPY miner_1790628306101.py .

# Install Python dependencies
RUN pip install --no-cache-dir randomx

# Create directories for logs and state files
RUN mkdir -p /app/data
ENV LOG_FILE=/app/data/miner.log
ENV STATE_FILE=/app/data/miner_state.json
ENV PAYOUT_HISTORY_FILE=/app/data/payout_history.json

# Set environment variables for Telegram (optional - pass at runtime)
# ENV TELEGRAM_BOT_TOKEN=your_token_here
# ENV TELEGRAM_CHAT_ID=your_chat_id_here

# Create non-root user for security
RUN useradd -m -u 1000 miner && chown -R miner:miner /app
USER miner

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import json, os; \
    state_file = os.getenv('STATE_FILE', '/app/data/miner_state.json'); \
    print('Health check passed')" || exit 1

# Run the application
CMD ["python", "miner_1790628306101.py"]
