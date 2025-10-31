from PIL import Image
import os

# 🔧 Dossier racine à traiter
SRC_DIR = "static/cabine/assets/fond/"
TARGET_SIZE = (1024, 1024)

# Extensions acceptées
EXTS = (".png", ".jpg", ".jpeg")

# Parcours récursif
for root, dirs, files in os.walk(SRC_DIR):
    for filename in files:
        if not filename.lower().endswith(EXTS):
            continue

        path = os.path.join(root, filename)
        try:
            with Image.open(path) as img:
                img = img.convert("RGBA") if filename.lower().endswith(".png") else img.convert("RGB")
                img = img.resize(TARGET_SIZE, Image.LANCZOS)
                img.save(path, optimize=True)
                print(f"✅ {path} → {TARGET_SIZE}")
        except Exception as e:
            print(f"⚠️ Erreur sur {path}: {e}")
