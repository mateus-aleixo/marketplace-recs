FROM python:3.12-slim

# LightGBM's wheel links the GNU OpenMP runtime, which slim images do not ship.
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[serve]"

# The ranker and its arrays are built from the data by `python -m marketplace_recs.online
# export`. CI builds without them, and the API then answers 503 instead of failing to start.
COPY model ./model
ENV RECS_MODEL_DIR=/app/model PORT=8080

EXPOSE 8080
# uvloop, not plain asyncio: see README, "Serving".
CMD uvicorn marketplace_recs.serve.app:app --host 0.0.0.0 --port ${PORT} \
    --loop uvloop --http httptools --no-access-log --workers ${WEB_CONCURRENCY:-1}
