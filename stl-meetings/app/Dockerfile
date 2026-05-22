FROM python:3.14.5-slim-trixie
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
    curl \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libffi-dev \
    shared-mime-info \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
RUN curl -LsSf https://astral.sh/uv/install.sh | UV_UNMANAGED_INSTALL="/usr/local/bin" sh
COPY pyproject.toml uv.lock *.py .
COPY templates templates
RUN uv sync
RUN mkdir -p /app/data /app/pdfs
EXPOSE 8000
CMD ["uv", "run", "gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "120", "main:app"]
