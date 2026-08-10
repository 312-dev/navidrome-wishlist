# Library Wishlist. See README.md for the environment variable table and the
# volume layout.
FROM python:3.12-slim

WORKDIR /app

# Dependencies first so that editing code does not bust the layer cache.
COPY pyproject.toml ./
RUN pip install --no-cache-dir flask jinja2 waitress

# The CSS is compiled and committed rather than built here, so the runtime image
# needs no Node toolchain.
COPY libwish ./libwish
COPY cookie_broker.py ./

# The database, the cookie jar and the music library all live on mounted
# volumes, never in the image. The mountpoints are created up front so a fresh
# named volume arrives with the right owner.
RUN useradd --create-home --uid 1000 wishlist \
    && mkdir -p /config /music \
    && chown -R wishlist:wishlist /app /config /music
USER wishlist

ENV LW_CONFIG_DIR=/config \
    LW_MUSIC_DIR=/music \
    LW_HOST=0.0.0.0 \
    LW_PORT=8080 \
    PYTHONUNBUFFERED=1

VOLUME ["/config", "/music"]
EXPOSE 8080

CMD ["python", "-m", "libwish", "serve"]
