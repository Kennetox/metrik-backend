"""Create compact WebP thumbnails for product images that already exist.

Run once from the Render Shell after deploying the thumbnail support:
    python scripts/backfill_product_thumbnails.py
"""

import sys
from pathlib import Path

from PIL import UnidentifiedImageError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services import storage


def main() -> None:
    root = storage.get_product_images_dir()
    created = 0
    skipped = 0
    failed = 0
    for source in root.rglob("*"):
        if not source.is_file() or "thumbnails" in source.parts:
            continue
        if source.suffix.lower() not in storage.ALLOWED_EXTENSIONS:
            continue
        thumbnail = storage.build_product_thumbnail_path(source)
        if thumbnail.exists():
            skipped += 1
            continue
        try:
            storage.create_product_thumbnail(source, thumbnail)
            created += 1
        except (UnidentifiedImageError, OSError, ValueError):
            failed += 1
            print(f"No se pudo crear miniatura: {source}")
    print(f"Miniaturas creadas={created}, existentes={skipped}, fallidas={failed}")


if __name__ == "__main__":
    main()
