from mood_speculator_v2 import app, db
import os

# s'assurer que le dossier instance/ existe (pour SQLite)
os.makedirs(os.path.join(os.path.dirname(__file__), "instance"), exist_ok=True)

with app.app_context():
    db.create_all()
