FROM python:3.11-slim

# Emoji font + curl needed for playwright install-deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-noto-color-emoji curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium + all its OS deps in one step (~300 MB, no Firefox/WebKit)
RUN playwright install --with-deps chromium

COPY . .

RUN mkdir -p tmp outputs

EXPOSE 8080

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
