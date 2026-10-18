FROM python:3.11-slim-bookworm
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
RUN python -m patchright install --with-deps chrome
RUN apt-get update && apt-get install -y --no-install-recommends xvfb && rm -rf /var/lib/apt/lists/*
COPY . .
CMD ["sh", "-c", "Xvfb :99 -screen 0 1280x800x24 -nolisten tcp & export DISPLAY=:99; sleep 2; exec python app.py"]
