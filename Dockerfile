# Dockerfile for Black House Bot
FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && \
    pip install -r requirements.txt
COPY bot_blackhouse.py /app/bot_blackhouse.py
CMD ["python", "bot_blackhouse.py"]
