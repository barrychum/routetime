FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY app.py .
COPY templates/ templates/

# Volume for persistent routes.json
VOLUME /data
ENV DATA_DIR=/data

EXPOSE 5000

CMD ["python", "app.py"]
