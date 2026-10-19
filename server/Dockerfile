FROM python:3.10-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./

# База и картинки живут в томе ./data, не в образе.
ENV DATA_DIR=/app/data AION_API_HOST=0.0.0.0 PYTHONUNBUFFERED=1
VOLUME ["/app/data"]
EXPOSE 8788

CMD ["python", "bot.py"]
