"""Stream sinks for Amazon Seller."""

import json
from datetime import datetime, timezone
from typing import Any

from hotglue_etl_exceptions import InvalidPayloadError
from sp_api.api import OrdersV0, OrdersV20260101

from target_amazon_seller.client import (
    MFN_CHANNEL_CODE,
    SP_API_ERRORS,
    AmazonSellerSink,
    sp_throttle_retry,
    to_number,
)


class ProductsSink(AmazonSellerSink):
    """Price updates on existing listings (no creates)."""

    name = "Products"

    @staticmethod
    def iter_variants(record: dict) -> list[dict[str, Any]]:
        """Variants carrying sku and price; a record without ``variants`` counts as one variant."""
        return record.get("variants") or [{"sku": record.get("sku"), "price": record.get("price")}]

    @staticmethod
    def price_patch_body(
        product_type: str,
        marketplace_id: str,
        currency: str,
        price: float,
    ) -> dict[str, Any]:
        """Patch body for ``/attributes/purchasable_offer``."""
        return {
            "productType": product_type,
            "patches": [
                {
                    "op": "replace",
                    "path": "/attributes/purchasable_offer",
                    "value": [
                        {
                            "marketplace_id": marketplace_id,
                            "currency": currency,
                            "our_price": [{"schedule": [{"value_with_tax": price}]}],
                        }
                    ],
                }
            ],
        }

    def upsert_record(self, record: dict, context: dict) -> tuple[Any, bool, dict]:
        """Patch ``purchasable_offer`` for each variant SKU on the record.

        A variant ``id`` is used as the SKU when ``sku`` is missing, because the unified read
        sets both to the seller SKU.
        """
        last_id = None
        for variant in self.iter_variants(record):
            sku = variant.get("sku") or variant.get("id")
            price = variant.get("price")
            if not sku:
                self.logger.warning("Skipping Products variant without sku or id")
                continue
            if price is None or price == "":
                self.logger.warning("Skipping Products variant sku=%s without price", sku)
                continue

            listing = self.get_listing_context(sku)
            if not listing.currency_code:
                raise InvalidPayloadError(f"Listing for SKU {sku!r} has no offer to update the price on")
            body = self.price_patch_body(
                product_type=listing.product_type,
                marketplace_id=self.marketplace_id,
                currency=listing.currency_code,
                price=to_number(price, float, "price"),
            )
            payload = self.patch_listings(sku, body)
            last_id = payload.get("submissionId") or sku
        if last_id is None:
            raise InvalidPayloadError("Products record has no sku/price pairs to update")
        return last_id, True, {}


class UpdateInventorySink(AmazonSellerSink):
    """MFN inventory and handling time on existing listings."""

    name = "UpdateInventory"

    OPERATIONS = ("set", "add", "subtract")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._mfn_quantity_written: dict[str, int] = {}

    @staticmethod
    def resolve_quantity(current: int | None, operation: str, quantity: int) -> int:
        """Compute the absolute MFN quantity for ``set``, ``add``, or ``subtract``."""
        if operation == "set":
            return quantity
        if current is None:
            raise InvalidPayloadError(
                f"Cannot apply inventory operation {operation!r}: "
                "getListingsItem did not return an MFN quantity"
            )
        if operation == "add":
            return current + quantity
        return current - quantity

    @staticmethod
    def inventory_patch_body(
        product_type: str,
        marketplace_id: str,
        quantity: int,
        lead_time_to_ship_max_days: int | None,
    ) -> dict[str, Any]:
        """Patch body for MFN ``/attributes/fulfillment_availability``."""
        availability: dict[str, Any] = {
            "marketplace_id": marketplace_id,
            "fulfillment_channel_code": MFN_CHANNEL_CODE,
            "quantity": quantity,
        }
        if lead_time_to_ship_max_days is not None:
            availability["lead_time_to_ship_max_days"] = lead_time_to_ship_max_days

        return {
            "productType": product_type,
            "patches": [
                {
                    "op": "replace",
                    "path": "/attributes/fulfillment_availability",
                    "value": [availability],
                }
            ],
        }

    def upsert_record(self, record: dict, context: dict) -> tuple[Any, bool, dict]:
        """Patch ``fulfillment_availability`` for the record SKU.

        Sending the MFN channel converts an Amazon-fulfilled listing to seller-fulfilled, so
        that only happens when the record sets ``switch_to_mfn``. ``replace`` drops any lead
        time missing from the patch, so the listing's current lead time is kept when the
        record has none. ``variant_id`` is used as the SKU when ``sku`` is missing, because
        the unified read sets variant ids to the seller SKU.

        Amazon applies accepted patches with a delay, so ``add`` and ``subtract`` start from
        the quantity this run last wrote for the SKU. Stock changed by another process during
        the run is not seen for those SKUs.
        """
        sku = record.get("sku") or record.get("variant_id")
        if not sku:
            raise InvalidPayloadError("UpdateInventory record is missing sku or variant_id")
        operation = (record.get("operation") or "set").lower()
        if operation not in self.OPERATIONS:
            raise InvalidPayloadError(f"Unrecognized inventory operation: {operation!r}")
        quantity = to_number(record.get("quantity"), int, "quantity")

        listing = self.get_listing_context(sku)
        if listing.is_amazon_fulfilled:
            if not record.get("switch_to_mfn"):
                raise InvalidPayloadError(
                    f"SKU {sku!r} is fulfilled by Amazon; set switch_to_mfn to convert it to seller-fulfilled"
                )
            self.logger.info("Switching sku=%s from Amazon-fulfilled to seller-fulfilled (MFN)", sku)
        current = self._mfn_quantity_written.get(sku, listing.mfn_quantity)
        new_quantity = self.resolve_quantity(current, operation, quantity)
        if new_quantity < 0:
            raise InvalidPayloadError(
                f"Inventory {operation!r} of {quantity} on sku={sku!r} would reduce MFN quantity "
                f"{current} below zero"
            )

        lead_time = record.get("lead_time_to_ship_max_days")
        if lead_time is None:
            lead_time = listing.lead_time_to_ship_max_days
        else:
            lead_time = to_number(lead_time, int, "lead_time_to_ship_max_days")

        body = self.inventory_patch_body(
            product_type=listing.product_type,
            marketplace_id=self.marketplace_id,
            quantity=new_quantity,
            lead_time_to_ship_max_days=lead_time,
        )
        payload = self.patch_listings(sku, body)
        self._mfn_quantity_written[sku] = new_quantity
        return payload.get("submissionId") or sku, True, {}


class FulfillmentsSink(AmazonSellerSink):
    """Shipment confirmation per package (partial or whole order)."""

    name = "Fulfillments"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._last_package_reference_id: dict[str, int] = {}

    def orders_client(self) -> OrdersV20260101:
        """Orders API v2026-01-01 client (replaces deprecated v0 read operations)."""
        return OrdersV20260101(credentials=self.sp_credentials(), marketplace=self.marketplace)

    def orders_client_v0(self) -> OrdersV0:
        """Orders API v0 client for ``confirmShipment`` (not on the v0 read deprecation list)."""
        return OrdersV0(credentials=self.sp_credentials(), marketplace=self.marketplace)

    @sp_throttle_retry()
    def get_order(self, order_id: str) -> dict[str, Any]:
        """Load an order with item fulfillment progress and existing packages."""
        self.logger.info("Loading order for order_id=%s", order_id)
        try:
            response = self.orders_client().get_order(
                order_id,
                includedData=["FULFILLMENT", "PACKAGES"],
            )
        except SP_API_ERRORS as exc:
            self.handle_sp_api_error(exc, f"getOrder order_id={order_id!r}")
        return response.payload["order"]

    def next_package_reference_id(self, order_id: str, order: dict[str, Any]) -> str:
        """Next numeric ``packageReferenceId``, unique within the order.

        Packages confirmed earlier in this run are tracked locally because they may not be
        returned by ``getOrder`` yet. Non-numeric ids from other shipping tools are ignored.
        """
        existing = [
            int(package["packageReferenceId"])
            for package in order.get("packages") or []
            if package["packageReferenceId"].isdigit()
        ]
        current = max([self._last_package_reference_id.get(order_id, 0), *existing]) + 1
        self._last_package_reference_id[order_id] = current
        return str(current)

    @staticmethod
    def record_order_items(line_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Map record ``line_items`` to ``confirmShipment`` orderItems."""
        if not line_items:
            raise InvalidPayloadError(
                "Fulfillment has empty line_items. Omit line_items to ship the whole order."
            )
        order_items = []
        for item in line_items:
            if not item.get("id"):
                raise InvalidPayloadError("Fulfillment line_item is missing id (Amazon OrderItemId)")
            quantity = to_number(item.get("quantity"), int, "line_item quantity")
            if quantity <= 0:
                raise InvalidPayloadError("Fulfillment line_item quantity must be positive")
            order_items.append({"orderItemId": str(item["id"]), "quantity": quantity})
        return order_items

    @staticmethod
    def unshipped_order_items(order: dict[str, Any]) -> list[dict[str, Any]]:
        """Package lines covering every unshipped unit on a ``getOrder`` order."""
        order_items = []
        for item in order["orderItems"]:
            remaining = (item.get("fulfillment") or {}).get("quantityUnfulfilled", 0)
            if remaining > 0:
                order_items.append({"orderItemId": item["orderItemId"], "quantity": remaining})
        if not order_items:
            raise InvalidPayloadError(f"No unshipped order items found for order {order['orderId']!r}")
        return order_items

    @sp_throttle_retry()
    def confirm_shipment(self, order_id: str, package_detail: dict[str, Any]) -> str:
        """Call Orders ``confirmShipment``; returns order id on success."""
        self.logger.info("Confirming shipment order_id=%s package=%s", order_id, json.dumps(package_detail))
        try:
            self.orders_client_v0().confirm_shipment(
                order_id,
                marketplaceId=self.marketplace_id,
                packageDetail=package_detail,
            )
        except SP_API_ERRORS as exc:
            self.handle_sp_api_error(exc, f"confirmShipment order_id={order_id!r}")
        return order_id

    def upsert_record(self, record: dict, context: dict) -> tuple[Any, bool, dict]:
        """Confirm shipment for one package on an order; omit ``line_items`` to ship the rest."""
        order_id = record.get("order_id")
        if not order_id:
            raise InvalidPayloadError("Fulfillment record is missing order_id")
        carrier = record.get("carrier")
        if not carrier:
            raise InvalidPayloadError(f"Fulfillment for order {order_id!r} is missing carrier")
        tracking_number = record.get("tracking_number")
        if not tracking_number:
            raise InvalidPayloadError(f"Fulfillment for order {order_id!r} is missing tracking_number")
        line_items = record.get("line_items")
        record_items = self.record_order_items(line_items) if line_items is not None else None

        order = self.get_order(order_id)
        package_detail: dict[str, Any] = {
            "packageReferenceId": self.next_package_reference_id(order_id, order),
            "carrierCode": str(carrier),
            "shippingMethod": record.get("shipping_method") or "Standard",
            "trackingNumber": str(tracking_number),
            "shipDate": record.get("ship_date") or datetime.now(timezone.utc).isoformat(),
            "orderItems": record_items or self.unshipped_order_items(order),
        }
        if record.get("carrier_name"):
            package_detail["carrierName"] = str(record["carrier_name"])

        return self.confirm_shipment(order_id, package_detail), True, {}
