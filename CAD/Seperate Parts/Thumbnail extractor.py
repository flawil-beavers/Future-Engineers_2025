import zipfile
import os

def extract_thumbnail(path):
    try:
        with zipfile.ZipFile(path, 'r') as z:
            # Kandidaten-Namen, die PrusaSlicer üblicherweise verwendet
            possible_names = [
                "Metadata/thumbnail.png",
                "thumbnail.png",
                "preview.png",
                "metadata/thumbnail.png",
            ]

            for name in possible_names:
                if name in z.namelist():
                    out_path = os.path.splitext(path)[0] + "_thumbnail.png"
                    with open(out_path, "wb") as f:
                        f.write(z.read(name))
                    print(f"✔ Thumbnail extracted: {out_path}")
                    return

        print(f"⚠ Kein Thumbnail gefunden in {path}")
    except Exception as e:
        print(f"❌ Fehler bei {path}: {e}")


if __name__ == "__main__":
    for file in os.listdir("."):
        if file.lower().endswith(".3mf"):
            extract_thumbnail(file)
