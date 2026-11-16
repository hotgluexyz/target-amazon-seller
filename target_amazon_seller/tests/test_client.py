"""Tests for listings GET payload parsing and shared SP-API call handling."""

import logging

import pytest
from hotglue_etl_exceptions import InvalidPayloadError
from sp_api.base import ApiResponse
from sp_api.base.exceptions import SellingApiBadRequestException, SellingApiRequestThrottledException

from target_amazon_seller.client import AmazonSellerSink, listing_context_from_payload


class _FakeListingsClient:
    def __init__(self, errors: list[Exception], payload: dict | None = None) -> None:
        self.errors = errors
        self.payload = payload or {"status": "ACCEPTED", "submissionId": "sub-1"}
        self.calls = 0

    def patch_listings_item(self, *args, **kwargs) -> ApiResponse:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return ApiResponse(payload=self.payload)


def _sink_with_client(monkeypatch, client: _FakeListingsClient) -> AmazonSellerSink:
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    monkeypatch.setattr(AmazonSellerSink, "selling_partner_id", "A1TESTSELLERID")
    monkeypatch.setattr(AmazonSellerSink, "marketplace_id", "ATVPDKIKX0DER")
    sink = AmazonSellerSink.__new__(AmazonSellerSink)
    sink.logger = logging.getLogger("test")
    monkeypatch.setattr(sink, "listings_client", lambda: client)
    return sink


def test_throttled_call_is_retried(monkeypatch):
    client = _FakeListingsClient([SellingApiRequestThrottledException([{"message": "throttled"}], None)])
    payload = _sink_with_client(monkeypatch, client).patch_listings("SKU-1", {})
    assert payload["submissionId"] == "sub-1"
    assert client.calls == 2


def test_bad_request_is_not_retried(monkeypatch):
    client = _FakeListingsClient([SellingApiBadRequestException([{"message": "bad"}])])
    with pytest.raises(InvalidPayloadError, match="patchListingsItem sku='SKU-1'"):
        _sink_with_client(monkeypatch, client).patch_listings("SKU-1", {})
    assert client.calls == 1


def test_invalid_patch_submission_fails_with_issues(monkeypatch):
    payload = {
        "status": "INVALID",
        "submissionId": "sub-1",
        "issues": [{"code": "90220", "message": "price is out of range", "severity": "ERROR"}],
    }
    client = _FakeListingsClient([], payload)
    with pytest.raises(InvalidPayloadError, match="INVALID.*price is out of range"):
        _sink_with_client(monkeypatch, client).patch_listings("SKU-1", {})


def test_listing_context_reads_mfn_listing():
    payload = {
        "summaries": [{"productType": "LUGGAGE"}],
        "offers": [{"offerType": "B2C", "price": {"currencyCode": "CAD", "amount": "10.00"}}],
        "fulfillmentAvailability": [{"fulfillmentChannelCode": "DEFAULT", "quantity": 12}],
        "attributes": {
            "fulfillment_availability": [
                {"fulfillment_channel_code": "DEFAULT", "quantity": 12, "lead_time_to_ship_max_days": 3},
            ]
        },
    }
    context = listing_context_from_payload("SKU-001", payload)
    assert context.product_type == "LUGGAGE"
    assert context.currency_code == "CAD"
    assert context.mfn_quantity == 12
    assert context.lead_time_to_ship_max_days == 3
    assert context.is_amazon_fulfilled is False


def test_listing_context_detects_amazon_fulfilled_listing():
    payload = {
        "summaries": [{"productType": "PRODUCT"}],
        "fulfillmentAvailability": [{"fulfillmentChannelCode": "AMAZON_NA"}],
    }
    context = listing_context_from_payload("SKU-002", payload)
    assert context.is_amazon_fulfilled is True
    assert context.mfn_quantity is None
    assert context.currency_code is None


def test_listing_context_requires_product_type():
    with pytest.raises(InvalidPayloadError, match="productType"):
        listing_context_from_payload("SKU-003", {"summaries": []})
