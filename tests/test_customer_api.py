from conftest import CUSTOMER_API_URL as URL, assert_error


# ---- Read ----------------------------------------------------------------------------------------

def test_get_existing_customer(http):
    r = http.get(f"{URL}/C001")
    assert r.status_code == 200
    body = r.json()
    assert body["customer_id"] == "C001"
    assert body["name"] == "Somchai Jaidee"
    assert body["tier"] == "GOLD"
    assert r.headers["x-correlation-id"]


def test_unknown_customer_returns_404(http):
    err = assert_error(http.get(f"{URL}/C999"), 404, "CUSTOMER_NOT_FOUND")
    assert "C999" in err["message"]


def test_malformed_customer_id_returns_400(http):
    err = assert_error(http.get(f"{URL}/not-an-id"), 400, "VALIDATION_ERROR")
    assert err["details"][0]["location"] == "path"


def test_list_customers_is_paginated(http):
    r = http.get(URL, params={"limit": 2, "offset": 0})
    assert r.status_code == 200
    body = r.json()
    assert len(body["data"]) == 2
    assert body["pagination"]["limit"] == 2
    assert body["pagination"]["total"] >= 5


def test_list_limit_out_of_range_returns_400(http):
    assert_error(http.get(URL, params={"limit": 500}), 400, "VALIDATION_ERROR")


def test_filter_by_email(http):
    r = http.get(URL, params={"email": "SOMCHAI.J@example.com"})
    assert [c["customer_id"] for c in r.json()["data"]] == ["C001"]


def test_correlation_id_is_propagated(http):
    r = http.get(f"{URL}/C001", headers={"X-Correlation-ID": "test-corr-123"})
    assert r.headers["x-correlation-id"] == "test-corr-123"


# ---- Create --------------------------------------------------------------------------------------

def test_create_customer(http, unique_email):
    r = http.post(URL, json={"name": "Test User", "email": unique_email, "tier": "STANDARD"})
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["customer_id"].startswith("C")
    assert r.headers["location"].endswith(f"/customers/{created['customer_id']}")
    assert http.get(f"{URL}/{created['customer_id']}").json()["email"] == unique_email


def test_duplicate_email_returns_409(http):
    assert_error(http.post(URL, json={"name": "Dup", "email": "somchai.j@example.com"}),
                 409, "CUSTOMER_EMAIL_EXISTS")


def test_invalid_body_returns_400_with_field_details(http):
    err = assert_error(http.post(URL, json={"name": "", "email": "not-an-email", "tier": "DIAMOND"}),
                       400, "VALIDATION_ERROR")
    fields = {d["field"] for d in err["details"]}
    assert {"name", "email", "tier"} <= fields


def test_unknown_fields_are_rejected(http, unique_email):
    assert_error(http.post(URL, json={"name": "X", "email": unique_email, "customer_id": "C123"}),
                 400, "VALIDATION_ERROR")
