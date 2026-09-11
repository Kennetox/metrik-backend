import asyncio
from io import BytesIO

from fastapi import UploadFile
from PIL import Image

from services import storage


def test_product_image_creates_compact_webp_thumbnail(monkeypatch, tmp_path):
    monkeypatch.setenv("PRODUCT_UPLOAD_DIR", str(tmp_path / "product-images"))
    original = BytesIO()
    Image.new("RGB", (1800, 900), "navy").save(original, format="JPEG", quality=96)
    upload = UploadFile(filename="product.jpg", file=BytesIO(original.getvalue()))

    result = asyncio.run(storage.save_product_image(upload, tenant_id=7))

    image_root = tmp_path / "product-images" / "7"
    assert (image_root / result.filename).is_file()
    assert result.thumb_url.startswith("/uploads/product-images/7/thumbnails/thumb-")
    thumb_name = result.thumb_url.rsplit("/", 1)[-1]
    with Image.open(image_root / "thumbnails" / thumb_name) as thumbnail:
        assert thumbnail.format == "WEBP"
        assert max(thumbnail.size) <= storage.PRODUCT_THUMBNAIL_MAX_EDGE
