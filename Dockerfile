FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 closeready \
    && useradd --uid 10001 --gid closeready --no-create-home --home-dir /app --shell /usr/sbin/nologin closeready \
    && mkdir -p /data \
    && chown closeready:closeready /app /data

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=closeready:closeready closeready ./closeready

USER closeready
EXPOSE 8000

CMD ["python", "-m", "uvicorn", "closeready.api:from_env", "--factory", "--host", "0.0.0.0", "--port", "8000"]
