FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir websockets
COPY app/ /app/
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data
