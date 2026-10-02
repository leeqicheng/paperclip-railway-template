# Build upstream Paperclip from a pinned ref.
FROM node:24-trixie-slim AS paperclip-build
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    gcc \
    libc6-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*
RUN corepack enable

# --- Rust toolchain for paperclip-runner (required since v2026.9xx) ---
# Mirrors upstream paperclipai/paperclip Dockerfile: version-pinned,
# checksum-verified rustup; the runner's rust-toolchain.toml picks the compiler.
ENV RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    PATH=/usr/local/cargo/bin:$PATH
ARG RUSTUP_VERSION=1.29.0
ARG RUSTUP_SHA256_AMD64=4acc9acc76d5079515b46346a485974457b5a79893cfb01112423c89aeb5aa10
ARG RUSTUP_SHA256_ARM64=9732d6c5e2a098d3521fca8145d826ae0aaa067ef2385ead08e6feac88fa5792
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) rustTarget="x86_64-unknown-linux-gnu"; sha256="$RUSTUP_SHA256_AMD64" ;; \
      arm64) rustTarget="aarch64-unknown-linux-gnu"; sha256="$RUSTUP_SHA256_ARM64" ;; \
      *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSLo /tmp/rustup-init "https://static.rust-lang.org/rustup/archive/${RUSTUP_VERSION}/${rustTarget}/rustup-init"; \
    echo "${sha256}  /tmp/rustup-init" | sha256sum -c -; \
    chmod +x /tmp/rustup-init; \
    /tmp/rustup-init -y --no-modify-path --profile minimal --default-toolchain none; \
    rm /tmp/rustup-init

ARG PAPERCLIP_REPO=https://github.com/paperclipai/paperclip.git
ARG PAPERCLIP_REF=v2026.1001.0

WORKDIR /paperclip
RUN git clone --depth 1 --branch "${PAPERCLIP_REF}" "${PAPERCLIP_REPO}" .
# Install the exact Rust compiler pinned by the runner package.
RUN cd packages/paperclip-runner && rustup show
RUN pnpm install --frozen-lockfile
RUN pnpm --filter @paperclipai/ui build
RUN pnpm --filter @paperclipai/plugin-sdk build
ENV NODE_OPTIONS=--max-old-space-size=4096
# Uncomment if the Railway builder runs out of memory while compiling Rust:
# ENV CARGO_BUILD_JOBS=2
RUN pnpm --filter @paperclipai/server build
RUN test -f server/dist/index.js
# The compiled runner is already vendored into server/dist; drop the multi-GB
# Rust build cache so it is not copied into the runtime image.
RUN rm -rf packages/paperclip-runner/runner/target

# Runtime image (direct Paperclip server, no wrapper).
# Paperclip requires Node >= 24.11 at runtime too.
FROM node:24-trixie
ENV NODE_ENV=production
ENV CLAUDE_CODE_BUBBLEWRAP=1
# Match upstream production image defaults (paperclipai/paperclip Dockerfile) so
# agent tooling, OpenCode, and config paths behave the same in containers.
ENV HOME=/paperclip \
    PAPERCLIP_INSTANCE_ID=default \
    PAPERCLIP_CONFIG=/paperclip/instances/default/config.json \
    OPENCODE_ALLOW_ALL_MODELS=true \
    GEMINI_SANDBOX=false

# mariadb-client: lets agents query the non-prod MySQL database (read-only user).
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    jq \
    openssh-client \
    gh \
    mariadb-client \
    ripgrep \
    tini \
    && rm -rf /var/lib/apt/lists/*
RUN corepack enable

WORKDIR /app
COPY --from=paperclip-build /paperclip /app

WORKDIR /wrapper
COPY package.json /wrapper/package.json
RUN npm install --omit=dev && npm cache clean --force
COPY src /wrapper/src
COPY scripts/entrypoint.sh /wrapper/entrypoint.sh
COPY scripts/bootstrap-ceo.mjs /wrapper/template/bootstrap-ceo.mjs
RUN chmod +x /wrapper/entrypoint.sh

# Optional local adapters/tools parity with upstream Dockerfile.
# Pinned (not @latest): an unpinned upstream release could break builds for
# every new deploy of this template with no warning. Bump deliberately.
RUN npm install --global --omit=dev \
    @anthropic-ai/claude-code@2.1.280 \
    @openai/codex@0.156.1 \
    opencode-ai@1.18.32 \
    @google/gemini-cli@0.60.0
RUN npm install --global --omit=dev tsx@4.23.15
# Headless Chromium + Playwright so agents can view pages and take screenshots.
# Headless Chromium + Playwright + screenshot helper for agents.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
WORKDIR /opt/agent-tools
COPY scripts/shot.mjs /opt/agent-tools/shot.mjs
COPY scripts/session.mjs /opt/agent-tools/session.mjs
RUN npm init -y >/dev/null \
    && npm install playwright@1.63.0 @axe-core/playwright@4 \
    && npx playwright install --with-deps chromium \
    && chmod -R a+rx /opt/agent-tools /ms-playwright
RUN npm install -g @railway/cli

RUN mkdir -p /paperclip \
    && chown -R node:node /app /paperclip /wrapper

# Railway sets PORT at runtime and this process binds to it.
# Entrypoint runs as root, fixes /paperclip volume permissions, then execs as node.
EXPOSE 3100
# tini, not node, is PID 1. The entrypoint ends in `exec`, so without an init
# node inherits PID 1 and never wait()s the orphans the kernel re-parents onto
# it — agent runs spawn git/claude/esbuild/sh descendants that outlive their
# leader, so they pile up as permanent zombies until the cgroup pid limit is
# exhausted and every fork() in the container fails.
ENTRYPOINT ["/usr/bin/tini", "--", "/wrapper/entrypoint.sh"]
CMD ["node", "/wrapper/src/server.js"]
