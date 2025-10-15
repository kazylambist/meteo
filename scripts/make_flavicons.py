from pathlib import Path
from PIL import Image

SRC = Path("static/img/zeus_flavicon.png")
OUT = Path("static/img")
OUT.mkdir(parents=True, exist_ok=True)

img = Image.open(SRC).convert("RGBA")

for s in (16, 32, 48, 64, 128, 256):
    img.resize((s, s), Image.LANCZOS).save(OUT / f"favicon-{s}.png")

img.save(OUT / "favicon.ico",
         sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])
print("Favicons générés dans static/img/")
