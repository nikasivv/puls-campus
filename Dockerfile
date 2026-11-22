FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000 DATABASE_PATH=/data/campus.db DEMO_MODE=1
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core ca-certificates \
  && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt requirements-server.txt ./
RUN pip install --no-cache-dir -r requirements-server.txt
COPY . .
RUN mkdir -p /data && useradd --create-home --uid 10001 campus && chown -R campus:campus /data /app
USER campus
EXPOSE 8000
CMD ["sh", "-c", "gunicorn --workers 1 --threads 4 --timeout 60 --bind 0.0.0.0:${PORT:-8000} wsgi:app"]
