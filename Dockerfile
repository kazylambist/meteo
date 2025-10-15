FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y build-essential && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install -U pip wheel && pip install -r requirements.txt
COPY . .

# Choisis la ligne CMD selon ton framework :
# Flask (WSGI)      : mood-speculator-v2:app      → remplace par ton module/objet

CMD ["gunicorn","-k","uvicorn.workers.UvicornWorker","-b","0.0.0.0:8000","mood-speculator-v2:app"]