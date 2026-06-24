FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    curl bash git ca-certificates \
    python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir "python-telegram-bot==21.*" httpx

RUN curl -fsSL https://x.ai/cli/install.sh | bash

ENV PATH="/root/.local/bin:/root/.grok/bin:/usr/local/bin:$PATH"

WORKDIR /app
COPY bot.py /app/bot.py
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

CMD ["/app/entrypoint.sh"]
