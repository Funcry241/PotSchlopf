FROM python:3.13.18-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY archiv.py hafen.py /app/
RUN install -d -m 0700 -o 65532 -g 65532 /archiv \
    && install -m 0600 -o 65532 -g 65532 /dev/null /archiv/.initialisiert

USER 65532:65532
ENTRYPOINT ["python3", "/app/hafen.py"]
