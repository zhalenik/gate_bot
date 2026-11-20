FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GATEBOT_DATA_DIR=/state \
    GATEBOT_LOG_DIR=/state/logs \
    HOST=0.0.0.0 \
    PORT=5000
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 568 gatebot \
    && useradd --uid 568 --gid 568 --no-create-home gatebot \
    && mkdir -p /state && chown 568:568 /state
COPY *.py ./
COPY VERSION ./
COPY storage ./storage
COPY templates ./templates
COPY static ./static
COPY resources ./resources
COPY examples ./examples
USER 568:568
EXPOSE 5000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '5000') + '/healthz', timeout=3)"
CMD ["python", "run.py"]
