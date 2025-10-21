FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

# (facultatif) build-essential pas utile si tu ne compiles rien
# RUN apt-get update && apt-get install -y build-essential && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -U pip wheel && pip install -r requirements.txt

COPY . .

# Fly écoute sur $PORT (8080)
ENV PORT=8080
EXPOSE 8080

# Gunicorn → bind 0.0.0.0:8080 et cible ton app Flask
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:8080", "mood_speculator_v2:app"]