FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY revos ./revos
RUN pip install --no-cache-dir .
COPY packs ./packs
COPY migrations ./migrations
