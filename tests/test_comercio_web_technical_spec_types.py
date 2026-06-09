from fastapi.testclient import TestClient


def _auth_headers(client: TestClient):
    resp = client.post(
        "/auth/login",
        json={"email": "master@kensar.com", "password": "2301"},
    )
    assert resp.status_code == 200
    token = resp.json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_comercio_web_technical_spec_types_endpoint_returns_common_types(client: TestClient):
    headers = _auth_headers(client)

    resp = client.get("/comercio-web/catalog/technical-spec-types", headers=headers)

    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert "Dimensiones" in data
    assert "Potencia" in data
    assert "Numero de cuerdas" in data
    assert "Patron polar" in data
