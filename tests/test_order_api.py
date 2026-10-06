from conftest import ORDER_API_URL as URL, assert_error

NEW_ORDER = {"customer_id": "C004", "items": [
    {"sku": "MSE-WL-01", "product_name": "Wireless Mouse", "quantity": 2, "unit_price": "590.00"},
    {"sku": "CBL-USBC-2M", "product_name": "USB-C Cable 2m", "quantity": 1, "unit_price": "490.00"}]}


# ---- Read ----------------------------------------------------------------------------------------

def test_get_order(http):
    r = http.get(f"{URL}/O1001")
    assert r.status_code == 200
    body = r.json()
    assert body["order_id"] == "O1001"
    assert body["customer_id"] == "C001"
    assert body["status"] == "DELIVERED"
    assert body["total_amount"] == "1880.00"          # money as a string, no float rounding
    assert len(body["items"]) == 2


def test_unknown_order_returns_404(http):
    assert_error(http.get(f"{URL}/O9999"), 404, "ORDER_NOT_FOUND")


def test_malformed_order_id_returns_400(http):
    assert_error(http.get(f"{URL}/1001"), 400, "VALIDATION_ERROR")


def test_latest_order_for_customer(http):
    r = http.get(URL, params={"customer_id": "C001", "limit": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["pagination"]["total"] >= 3
    latest = body["data"][0]
    assert latest["order_id"] == "O1006"
    assert latest["status"] == "SHIPPED"


def test_orders_are_newest_first(http):
    data = http.get(URL, params={"customer_id": "C001"}).json()["data"]
    created = [o["created_at"] for o in data]
    assert created == sorted(created, reverse=True)


def test_customer_without_orders_returns_empty_list(http):
    r = http.get(URL, params={"customer_id": "C999"})
    assert r.status_code == 200
    assert r.json()["data"] == []


def test_filter_by_status(http):
    data = http.get(URL, params={"status": "PENDING"}).json()["data"]
    assert data and all(o["status"] == "PENDING" for o in data)


def test_invalid_status_filter_returns_400(http):
    assert_error(http.get(URL, params={"status": "LOST"}), 400, "VALIDATION_ERROR")


# ---- Create & lifecycle --------------------------------------------------------------------------

def test_create_order_calculates_total(http):
    r = http.post(URL, json=NEW_ORDER)
    assert r.status_code == 201, r.text
    order = r.json()
    assert order["status"] == "PENDING"
    assert order["total_amount"] == "1670.00"
    assert [i["line_no"] for i in order["items"]] == [1, 2]
    assert r.headers["location"].endswith(f"/orders/{order['order_id']}")


def test_create_order_without_items_returns_400(http):
    assert_error(http.post(URL, json={"customer_id": "C001", "items": []}), 400, "VALIDATION_ERROR")


def test_create_order_with_negative_quantity_returns_400(http):
    bad = {"customer_id": "C001", "items": [{"sku": "X", "product_name": "X", "quantity": -1, "unit_price": "1"}]}
    assert_error(http.post(URL, json=bad), 400, "VALIDATION_ERROR")


def test_status_lifecycle_and_invalid_transition(http):
    order_id = http.post(URL, json=NEW_ORDER).json()["order_id"]

    for status in ("PAID", "SHIPPED"):
        r = http.patch(f"{URL}/{order_id}", json={"status": status})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == status

    err = assert_error(http.patch(f"{URL}/{order_id}", json={"status": "PENDING"}),
                       409, "INVALID_STATUS_TRANSITION")
    assert err["details"][0]["allowed"] == ["DELIVERED"]


def test_update_unknown_order_returns_404(http):
    assert_error(http.patch(f"{URL}/O9999", json={"status": "PAID"}), 404, "ORDER_NOT_FOUND")
