# Base image paling kecil
FROM python:3.11-slim

# Set folder kerja
WORKDIR /app

# Copy requirements dulu biar cache
COPY requirements.txt.

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy semua file bot
COPY . .

# Jalanin bot dengan optimasi RAM
CMD ["python", "-O", "bot.py"]
