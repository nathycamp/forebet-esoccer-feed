FROM python:3.11-slim-bookworm
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
RUN python -m patchright install --with-deps chrome
RUN apt-get update && apt-get install -y --no-install-recommends xvfb xauth && rm -rf /var/lib/apt/lists/*
COPY . .
CMD ["xvfb-run", "-a", "python", "app.py"]
