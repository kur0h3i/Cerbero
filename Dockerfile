# syntax=docker/dockerfile:1
FROM python:3.12-slim

# Metadatos OCI: Dis (y otros paneles) los usan como descripción del contenedor.
LABEL org.opencontainers.image.title="Cerbero" \
      org.opencontainers.image.description="Monitor de recursos, contenedores y accesos del servidor"

# MALLOC_ARENA_MAX: con varios hilos, glibc reserva una arena de memoria por hilo;
# limitarlas ahorra RAM (restricción de < 80 MB).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MALLOC_ARENA_MAX=2
WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
RUN pip install . && rm -rf /root/.cache

EXPOSE 9666
# Comprobación con un socket a pelo y sin ``site`` (-S): ~10 MB frente a los ~18
# de urllib, y su memoria cuenta dentro del límite del contenedor.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-S", "-c", "import socket; s = socket.create_connection(('127.0.0.1', 9666), 3); s.sendall(b'GET /api/health HTTP/1.0\\r\\n\\r\\n'); assert b' 200 ' in s.recv(64)"]

# Un único proceso sin workers extra. Sin access log: Dis consulta cada pocos segundos.
CMD ["uvicorn", "--factory", "app.main:create_app", "--host", "0.0.0.0", "--port", "9666", "--no-access-log"]
