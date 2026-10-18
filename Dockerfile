FROM python:3.11-slim-bookworm
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
RUN python -m playwright install --with-deps chromium
COPY . .
CMD ["python", "app.py"]
