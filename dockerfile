FROM python:3.11-slim

# Don't buffer output, don't write .pyc files
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Work directory inside the container
WORKDIR /app

# Install system dependencies (empty for now, extend if you need)
RUN apt-get update && apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the code
COPY . .

# Start the bot (change bot.py if your main file has a different name)
CMD ["python", "bot.py"]
