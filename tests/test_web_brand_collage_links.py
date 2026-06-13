import crud


def test_web_brand_collage_images_default_links_are_present():
    normalized = crud._normalize_web_brand_collage_images({})

    assert normalized["main"]["href"] == "/catalogo?brand=Yamaha"
    assert normalized["top_left"]["href"] == "/catalogo?brand=Pro%20DJ"
    assert normalized["top_right"]["href"] == "/catalogo?brand=Ritmo%20Musical"
    assert normalized["bottom"]["href"] == "/catalogo?brand=Spain"


def test_web_brand_collage_images_preserves_custom_links():
    normalized = crud._normalize_web_brand_collage_images(
        {
            "main": {
                "image_url": "/brands/collage/custom-main.webp",
                "href": "/catalogo?brand=Ayson",
            }
        }
    )

    assert normalized["main"]["image_url"] == "/brands/collage/custom-main.webp"
    assert normalized["main"]["href"] == "/catalogo?brand=Ayson"
