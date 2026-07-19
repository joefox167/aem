FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd -u 1000 -m aem && mkdir -p /data /config && chown -R aem /data /config
USER aem

ENV AEM_DB_PATH=/data/aem.db \
    AEM_CONFIG_FILE=/config/config.yaml \
    PYTHONUNBUFFERED=1

EXPOSE 8000
CMD ["uvicorn", "aem.main:app", "--host", "0.0.0.0", "--port", "8000"]
