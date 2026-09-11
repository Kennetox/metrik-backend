"""Create compact WebP thumbnails and register them for existing products.

Run once from the Render Shell after deploying the thumbnail support:
    python scripts/backfill_product_thumbnails.py
"""

import sys
from pathlib import Path
from urllib.parse import urlparse

from PIL import UnidentifiedImageError
from sqlalchemy import or_

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from database import SessionLocal
import models
from services import storage


def _resolve_source_path(product: models.Product) -> Path | None:
    """Resolve only files owned by this service; leave external URLs intact."""
    image_url = (product.image_url or "").strip()
    if not image_url:
        return None
    filename = Path(urlparse(image_url).path).name
    if not filename or Path(filename).suffix.lower() not in storage.ALLOWED_EXTENSIONS:
        return None
    return storage._get_base_dir(product.tenant_id) / filename


def main() -> None:
    db = SessionLocal()
    created = 0
    existing = 0
    failed = 0
    updated = 0
    missing = 0
    try:
        products = (
            db.query(models.Product)
            .filter(
                models.Product.image_url.is_not(None),
                models.Product.image_url != "",
                or_(
                    models.Product.image_thumb_url.is_(None),
                    models.Product.image_thumb_url == "",
                    models.Product.image_thumb_url == models.Product.image_url,
                ),
            )
            .all()
        )
        for product in products:
            source = _resolve_source_path(product)
            if source is None or not source.is_file():
                missing += 1
                continue
            thumbnail = storage.build_product_thumbnail_path(source)
            try:
                if thumbnail.exists():
                    existing += 1
                else:
                    storage.create_product_thumbnail(source, thumbnail)
                    created += 1
                product.image_thumb_url = storage._build_public_url(
                    f"thumbnails/{thumbnail.name}", product.tenant_id
                )
                updated += 1
            except (UnidentifiedImageError, OSError, ValueError):
                failed += 1
                print(f"No se pudo crear miniatura: producto={product.id} archivo={source}")
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    print(
        "Miniaturas creadas="
        f"{created}, existentes={existing}, productos actualizados={updated}, "
        f"archivos no encontrados={missing}, fallidas={failed}"
    )


if __name__ == "__main__":
    main()
