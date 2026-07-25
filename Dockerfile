# 5b live deep-dive service (Cloud Run target).
# Build from the REPO ROOT so src/ and output/feature_keys.json are in context:
#   docker build -f service/Dockerfile -t investigator-api .
FROM python:3.11-slim

WORKDIR /app

COPY service/requirements.txt service/requirements.txt
RUN pip install --no-cache-dir -r service/requirements.txt

# Only what the service imports: the investigator package + the canonical
# feature-key vocabulary + the app itself.
COPY src/ src/
COPY output/feature_keys.json output/feature_keys.json
COPY service/investigator_api.py service/investigator_api.py

# Cloud Run injects PORT; default 8080 for local docker run.
ENV PORT=8080
CMD ["sh", "-c", "uvicorn investigator_api:app --app-dir service --host 0.0.0.0 --port ${PORT}"]
