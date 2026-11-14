FROM mcr.microsoft.com/playwright/python:v1.60.0-noble
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV DEALS_DATA_DIR=/data DEALS_PORT=8050
VOLUME /data
EXPOSE 8050
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${DEALS_PORT}"]
