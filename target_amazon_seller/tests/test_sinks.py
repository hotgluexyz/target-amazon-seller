"""Tests for the Products, UpdateInventory, and Fulfillments sinks."""

import logging

import pytest
from hotglue_etl_exceptions import InvalidPayloadError

from target_amazon_seller.client import ListingContext
from target_amazon_seller.sinks import FulfillmentsSink, ProductsSink, UpdateInventorySink

ORDER_ID = "902-1845936-5435065"


def _listing(
    mfn_quantity: int | None = 10,
    lead_time: int | None = None,
    is_amazon_fulfilled: bool = False,
) -> ListingContext:
    return ListingContext("PRODUCT", "USD", mfn_quantity, lead_time, is_amazon_fulfilled)


def _inventory_sink(monkeypatch, listing: ListingContext) -> tuple[UpdateInventorySink, list[dict]]:
    sink = UpdateInventorySink.__new__(UpdateInventorySink)
    sink.logger = logging.getLogger("test")
    sink._mfn_quantity_written = {}
    patched: list[dict] = []
    monkeypatch.setattr(UpdateInventorySink, "marketplace_id", "ATVPDKIKX0DER")
    monkeypatch.setattr(sink, "get_listing_context", lambda sku: listing)
    monkeypatch.setattr(
        sink, "patch_listings", lambda sku, body: patched.append(body) or {"submissionId": "sub-1"}
    )
    return sink, patched


def _availability(body: dict) -> dict:
    return body["patches"][0]["value"][0]


def _fulfillments_sink() -> FulfillmentsSink:
    sink = FulfillmentsSink.__new__(FulfillmentsSink)
    sink._last_package_reference_id = {}
    return sink


def _shipping_sink(monkeypatch, order: dict) -> tuple[FulfillmentsSink, list[tuple[str, dict]]]:
    sink = _fulfillments_sink()
    confirmed: list[tuple[str, dict]] = []
    monkeypatch.setattr(sink, "get_order", lambda order_id: order)
    monkeypatch.setattr(
        sink,
        "confirm_shipment",
        lambda order_id, package: confirmed.append((order_id, package)) or order_id,
    )
    return sink, confirmed


def test_price_patch_shape():
    body = ProductsSink.price_patch_body(
        product_type="PRODUCT",
        marketplace_id="ATVPDKIKX0DER",
        currency="USD",
        price=19.99,
    )
    assert body["productType"] == "PRODUCT"
    patch = body["patches"][0]
    assert patch["path"] == "/attributes/purchasable_offer"
    offer = patch["value"][0]
    assert offer["currency"] == "USD"
    assert offer["our_price"][0]["schedule"][0]["value_with_tax"] == 19.99


def test_record_without_variants_is_one_variant():
    variants = ProductsSink.iter_variants({"sku": "SKU-1", "price": "5"})
    assert variants == [{"sku": "SKU-1", "price": "5"}]


def test_products_uses_variant_id_as_sku(monkeypatch):
    sink = ProductsSink.__new__(ProductsSink)
    sink.logger = logging.getLogger("test")
    patched_skus: list[str] = []
    monkeypatch.setattr(ProductsSink, "marketplace_id", "ATVPDKIKX0DER")
    monkeypatch.setattr(sink, "get_listing_context", lambda sku: _listing())
    monkeypatch.setattr(
        sink, "patch_listings", lambda sku, body: patched_skus.append(sku) or {"submissionId": "sub-1"}
    )
    sink.upsert_record({"id": "B0PARENT", "variants": [{"id": "SKU-1", "price": "9.99"}]}, {})
    assert patched_skus == ["SKU-1"]


def test_inventory_uses_variant_id_as_sku(monkeypatch):
    sink, patched = _inventory_sink(monkeypatch, _listing())
    looked_up: list[str] = []
    monkeypatch.setattr(sink, "get_listing_context", lambda sku: looked_up.append(sku) or _listing())
    sink.upsert_record({"variant_id": "SKU-1", "quantity": 5}, {})
    assert looked_up == ["SKU-1"]
    assert len(patched) == 1


def test_set_uses_record_quantity_directly():
    assert UpdateInventorySink.resolve_quantity(4, "set", 10) == 10


def test_subtract_computes_from_live_quantity():
    assert UpdateInventorySink.resolve_quantity(10, "subtract", 3) == 7


def test_add_requires_existing_mfn_quantity():
    with pytest.raises(InvalidPayloadError):
        UpdateInventorySink.resolve_quantity(None, "add", 1)


def test_repeated_subtract_starts_from_quantity_written_in_this_run(monkeypatch):
    sink, patched = _inventory_sink(monkeypatch, _listing(10))
    sink.upsert_record({"sku": "SKU-1", "operation": "subtract", "quantity": 3}, {})
    sink.upsert_record({"sku": "SKU-1", "operation": "subtract", "quantity": 2}, {})
    assert [_availability(body)["quantity"] for body in patched] == [7, 5]


def test_amazon_fulfilled_listing_requires_switch_to_mfn(monkeypatch):
    sink, patched = _inventory_sink(monkeypatch, _listing(None, is_amazon_fulfilled=True))
    with pytest.raises(InvalidPayloadError, match="switch_to_mfn"):
        sink.upsert_record({"sku": "SKU-1", "quantity": 5}, {})
    assert patched == []


def test_switch_to_mfn_patches_amazon_fulfilled_listing(monkeypatch):
    sink, patched = _inventory_sink(monkeypatch, _listing(None, is_amazon_fulfilled=True))
    sink.upsert_record({"sku": "SKU-1", "quantity": 5, "switch_to_mfn": True}, {})
    assert _availability(patched[0])["fulfillment_channel_code"] == "DEFAULT"
    assert _availability(patched[0])["quantity"] == 5


def test_keeps_listing_lead_time_when_record_has_none(monkeypatch):
    sink, patched = _inventory_sink(monkeypatch, _listing(lead_time=4))
    sink.upsert_record({"sku": "SKU-1", "quantity": 5}, {})
    assert _availability(patched[0])["lead_time_to_ship_max_days"] == 4


def test_record_lead_time_overrides_listing(monkeypatch):
    sink, patched = _inventory_sink(monkeypatch, _listing(lead_time=4))
    sink.upsert_record({"sku": "SKU-1", "quantity": 5, "lead_time_to_ship_max_days": "2"}, {})
    assert _availability(patched[0])["lead_time_to_ship_max_days"] == 2


def test_non_numeric_quantity_is_invalid_payload(monkeypatch):
    sink, _ = _inventory_sink(monkeypatch, _listing())
    with pytest.raises(InvalidPayloadError, match="quantity"):
        sink.upsert_record({"sku": "SKU-1", "quantity": "ten"}, {})


def test_record_order_items():
    items = FulfillmentsSink.record_order_items([{"id": "166417490198481", "quantity": 2}])
    assert items == [{"orderItemId": "166417490198481", "quantity": 2}]


def test_empty_line_items_rejected():
    with pytest.raises(InvalidPayloadError, match="empty line_items"):
        FulfillmentsSink.record_order_items([])


def test_unshipped_order_items_from_get_order():
    order = {
        "orderId": ORDER_ID,
        "orderItems": [
            {"orderItemId": "05015851154158", "fulfillment": {"quantityUnfulfilled": 2}},
            {"orderItemId": "999", "fulfillment": {"quantityUnfulfilled": 0}},
        ],
    }
    items = FulfillmentsSink.unshipped_order_items(order)
    assert items == [{"orderItemId": "05015851154158", "quantity": 2}]


def test_unshipped_order_items_rejects_fully_shipped_order():
    order = {"orderId": ORDER_ID, "orderItems": [{"orderItemId": "1", "fulfillment": {"quantityUnfulfilled": 0}}]}
    with pytest.raises(InvalidPayloadError, match="No unshipped"):
        FulfillmentsSink.unshipped_order_items(order)


def test_package_reference_id_follows_existing_packages():
    sink = _fulfillments_sink()
    order = {"packages": [{"packageReferenceId": "1"}, {"packageReferenceId": "2"}, {"packageReferenceId": "BS-77"}]}
    assert sink.next_package_reference_id(ORDER_ID, order) == "3"


def test_package_reference_id_counts_packages_confirmed_in_this_run():
    sink = _fulfillments_sink()
    assert sink.next_package_reference_id(ORDER_ID, {"packages": []}) == "1"
    assert sink.next_package_reference_id(ORDER_ID, {"packages": []}) == "2"


def test_fulfillment_confirms_record_line_items(monkeypatch):
    order = {"orderId": ORDER_ID, "orderItems": [], "packages": [{"packageReferenceId": "1"}]}
    sink, confirmed = _shipping_sink(monkeypatch, order)
    record = {
        "order_id": ORDER_ID,
        "carrier": "USPS",
        "tracking_number": "9400100000000000000000",
        "ship_date": "2026-11-16T12:00:00Z",
        "line_items": [{"id": "166417490198481", "quantity": 1}],
    }
    sink.upsert_record(record, {})
    assert confirmed == [
        (
            ORDER_ID,
            {
                "packageReferenceId": "2",
                "carrierCode": "USPS",
                "shippingMethod": "Standard",
                "trackingNumber": "9400100000000000000000",
                "shipDate": "2026-11-16T12:00:00Z",
                "orderItems": [{"orderItemId": "166417490198481", "quantity": 1}],
            },
        )
    ]


def test_fulfillment_without_line_items_ships_unfulfilled_units(monkeypatch):
    order = {
        "orderId": ORDER_ID,
        "orderItems": [{"orderItemId": "05015851154158", "fulfillment": {"quantityUnfulfilled": 2}}],
    }
    sink, confirmed = _shipping_sink(monkeypatch, order)
    sink.upsert_record({"order_id": ORDER_ID, "carrier": "UPS", "tracking_number": "1Z999"}, {})
    package = confirmed[0][1]
    assert package["packageReferenceId"] == "1"
    assert package["orderItems"] == [{"orderItemId": "05015851154158", "quantity": 2}]
