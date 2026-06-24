FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    curl \
    bash \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://x.ai/cli/install.sh | bash

ENV PATH="/root/.local/bin:/root/.grok/bin:$PATH"

CMD ["tail", "-f", "/dev/null"]
