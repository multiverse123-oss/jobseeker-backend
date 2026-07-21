FROM alpine:3.19

# Install system dependencies for PocketBase, Litestream, Python3, pip, and Chromium (for Playwright)
RUN apk add --no-cache \
    ca-certificates wget unzip \
    python3 py3-pip \
    chromium chromium-chromedriver \
    nss freetype harfbuzz \
    font-dejavu fontconfig \
    && rm -rf /var/cache/apk/*

# PocketBase
RUN wget -q -O /tmp/pb.zip https://github.com/pocketbase/pocketbase/releases/download/v0.22.21/pocketbase_0.22.21_linux_amd64.zip \
    && unzip /tmp/pb.zip -d /app && rm /tmp/pb.zip && chmod +x /app/pocketbase

# Litestream
RUN wget -q https://github.com/benbjohnson/litestream/releases/download/v0.3.13/litestream-v0.3.13-linux-amd64.tar.gz \
    && tar -xzf litestream-v0.3.13-linux-amd64.tar.gz -C /app && rm litestream-v0.3.13-linux-amd64.tar.gz && chmod +x /app/litestream

# Create a virtual environment for the worker
RUN python3 -m venv /app/worker-venv

# Copy worker requirements and install (this pulls Playwright driver – large, but inside the image)
COPY worker/requirements.txt /app/worker-requirements.txt
RUN /app/worker-venv/bin/pip install --no-cache-dir -r /app/worker-requirements.txt

# Copy the rest of the project
COPY run.sh /app/run.sh
COPY litestream.yml /app/litestream.yml
COPY worker/worker.py /app/worker.py

RUN chmod +x /app/run.sh && mkdir -p /app/pb_data

EXPOSE 8090
CMD ["/app/run.sh"]
