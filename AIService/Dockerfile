FROM node:22-bookworm-slim AS customer-web

WORKDIR /web
COPY h5/package.json h5/package-lock.json ./
RUN npm ci
COPY h5/index.html h5/quasar.config.js h5/tsconfig.json ./
COPY h5/src ./src
RUN npm run build

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY AIService/ai_service ./ai_service
COPY --from=customer-web /web/dist/spa ./ai_service/customer_web

# 固定并校验mpegts.js版本，正式环境从AIService本地提供，不依赖浏览器访问公网CDN。
RUN python -c "from base64 import b64encode; from hashlib import sha384; from pathlib import Path; from urllib.request import urlopen; url='https://cdn.jsdelivr.net/npm/mpegts.js@1.8.0/dist/mpegts.min.js'; expected='lUyV7hLicoTyv6bvfYqrGlhj26lFlaig1x0rD5/ntvHk+jiOX2oUTbNjrVE4cUXQ'; body=urlopen(url, timeout=30).read(); actual=b64encode(sha384(body).digest()).decode(); assert actual == expected, f'mpegts.js checksum mismatch: {actual}'; Path('/app/ai_service/web/mpegts.min.js').write_bytes(body)"
RUN python -c "from pathlib import Path; from urllib.request import urlopen; body=urlopen('https://cdn.jsdelivr.net/npm/mpegts.js@1.8.0/LICENSE', timeout=30).read(); Path('/app/ai_service/web/MPEGTS-LICENSE.txt').write_bytes(body)"

VOLUME ["/var/recordings"]
EXPOSE 11111
CMD ["python", "-m", "ai_service"]
