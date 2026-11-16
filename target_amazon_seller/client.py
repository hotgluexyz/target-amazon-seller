"""Base sink wrapping ``python-amazon-sp-api`` for Amazon Seller writes."""

import json
from dataclasses import dataclass
from typing import Any, NoReturn

from hotglue_etl_exceptions import InvalidCredentialsError, InvalidPayloadError
from hotglue_singer_sdk.target_sdk.client import HotglueSink
from sp_api.api import ListingsItems
from sp_api.auth.exceptions import AuthorizationError
from sp_api.base import Marketplaces
from sp_api.base.exceptions import (
    SellingApiBadRequestException,
    SellingApiException,
    SellingApiForbiddenException,
    SellingApiNotFoundException,
)
from sp_api.util import throttle_retry

MFN_CHANNEL_CODE = "DEFAULT"
SP_API_ERRORS = (SellingApiException, AuthorizationError)


def sp_throttle_retry():
    """Retry SP-API calls on throttling (429) only, waiting about 12s in total before giving up.

    Server errors are not retried because a timed-out ``confirmShipment`` may already have
    been applied. Each call returns a new decorator because ``throttle_retry`` keeps its
    attempt counter per decorator.
    """
    return throttle_retry(tries=5, delay=2)


@dataclass(frozen=True)
class ListingContext:
    """Listing fields loaded before ``patchListingsItem``."""

    product_type: str
    currency_code: str | None
    mfn_quantity: int | None
    lead_time_to_ship_max_days: int | None
    is_amazon_fulfilled: bool


def listing_context_from_payload(sku: str, payload: dict[str, Any]) -> ListingContext:
    """Build a ``ListingContext`` from a ``getListingsItem`` response payload.

    MFN quantity comes from ``fulfillmentAvailability`` (live sellable quantity), while
    lead time only exists on the submitted ``fulfillment_availability`` attribute.
    """
    summaries = payload.get("summaries") or []
    product_type = summaries[0].get("productType") if summaries else None
    if not product_type:
        raise InvalidPayloadError(f"Listing for SKU {sku!r} has no productType in getListingsItem response")

    availability = payload.get("fulfillmentAvailability") or []
    mfn_availability = next(
        (entry for entry in availability if entry.get("fulfillmentChannelCode") == MFN_CHANNEL_CODE),
        {},
    )
    mfn_attribute = next(
        (
            entry
            for entry in (payload.get("attributes") or {}).get("fulfillment_availability", [])
            if entry.get("fulfillment_channel_code") == MFN_CHANNEL_CODE
        ),
        {},
    )
    offers = payload.get("offers") or []

    return ListingContext(
        product_type=product_type,
        currency_code=offers[0]["price"]["currencyCode"] if offers else None,
        mfn_quantity=mfn_availability.get("quantity"),
        lead_time_to_ship_max_days=mfn_attribute.get("lead_time_to_ship_max_days"),
        is_amazon_fulfilled=any(
            entry.get("fulfillmentChannelCode") != MFN_CHANNEL_CODE for entry in availability
        ),
    )


def to_number(value: Any, cast: type, field: str) -> Any:
    """Cast a record value to ``float`` or ``int``, raising ``InvalidPayloadError`` when it does not fit.

    ``int`` accepts whole floats such as ``5.0`` or ``"5.0"`` and rejects ``2.9`` instead of
    truncating it.
    """
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise InvalidPayloadError(f"{field} must be a number, got {value!r}") from exc
    if cast is int:
        if not number.is_integer():
            raise InvalidPayloadError(f"{field} must be a whole number, got {value!r}")
        return int(number)
    return number


class AmazonSellerSink(HotglueSink):
    """Shared SP-API config, error mapping, and listing read/patch for Amazon Seller sinks."""

    LISTING_INCLUDED_DATA = ["summaries", "attributes", "offers", "fulfillmentAvailability"]

    def preprocess_record(self, record: dict, context: dict) -> dict:
        """Required SDK hook; validation happens in ``upsert_record``."""
        return record

    @property
    def marketplace(self) -> Marketplaces:
        """Configured Selling Partner marketplace enum."""
        code = (self.config.get("marketplace") or "US").strip().upper()
        return Marketplaces[code]

    @property
    def marketplace_id(self) -> str:
        """Marketplace id string for query parameters."""
        return self.marketplace.marketplace_id

    @property
    def selling_partner_id(self) -> str:
        """Merchant identifier required on Listings Items URLs."""
        return self.config["selling_partner_id"]

    def sp_credentials(self) -> dict[str, Any]:
        """Credential dict for ``python-amazon-sp-api``."""
        return {
            "refresh_token": self.config.get("refresh_token"),
            "lwa_app_id": self.config.get("lwa_client_id"),
            "lwa_client_secret": self.config.get("client_secret"),
            "aws_access_key": self.config.get("aws_access_key"),
            "aws_secret_key": self.config.get("aws_secret_key"),
            "role_arn": self.config.get("role_arn"),
        }

    def listings_client(self) -> ListingsItems:
        """Listings Items API client."""
        return ListingsItems(
            credentials=self.sp_credentials(),
            marketplace=self.marketplace,
        )

    def handle_sp_api_error(self, exc: Exception, label: str) -> NoReturn:
        """Map ``python-amazon-sp-api`` errors to hotglue error classes; re-raise anything else.

        ``label`` names the call and record key (e.g. ``getListingsItem sku='ABC'``) because
        Amazon error bodies do not say which SKU or order they refer to. ``AuthorizationError``
        is a failed LWA token exchange (bad client id, secret, or refresh token).
        """
        if isinstance(exc, (SellingApiForbiddenException, AuthorizationError)):
            raise InvalidCredentialsError(f"{label}: {exc}") from exc
        if isinstance(exc, (SellingApiBadRequestException, SellingApiNotFoundException)):
            raise InvalidPayloadError(f"{label}: {exc}") from exc
        raise exc

    @sp_throttle_retry()
    def get_listing_context(self, sku: str) -> ListingContext:
        """Load listing metadata before a patch; fail clearly when the SKU is not listed."""
        self.logger.info(
            "Resolving listing seller=%s marketplace=%s sku=%s",
            self.selling_partner_id,
            self.marketplace_id,
            sku,
        )
        try:
            response = self.listings_client().get_listings_item(
                self.selling_partner_id,
                sku,
                marketplaceIds=[self.marketplace_id],
                includedData=self.LISTING_INCLUDED_DATA,
            )
        except SP_API_ERRORS as exc:
            self.handle_sp_api_error(exc, f"getListingsItem sku={sku!r}")

        context = listing_context_from_payload(sku, response.payload or {})
        self.logger.info(
            "Resolved listing sku=%s productType=%s mfn_quantity=%s lead_time=%s "
            "currency=%s amazon_fulfilled=%s",
            sku,
            context.product_type,
            context.mfn_quantity,
            context.lead_time_to_ship_max_days,
            context.currency_code,
            context.is_amazon_fulfilled,
        )
        return context

    @sp_throttle_retry()
    def patch_listings(self, sku: str, body: dict[str, Any]) -> dict[str, Any]:
        """Submit ``patchListingsItem`` and return the response payload.

        Amazon answers 200 with ``status: INVALID`` and ``issues`` when it rejects a patch,
        so anything other than ``ACCEPTED`` fails the record.
        """
        self.logger.info("Patching listing sku=%s body=%s", sku, json.dumps(body))
        try:
            response = self.listings_client().patch_listings_item(
                self.selling_partner_id,
                sku,
                marketplaceIds=[self.marketplace_id],
                body=body,
            )
        except SP_API_ERRORS as exc:
            self.handle_sp_api_error(exc, f"patchListingsItem sku={sku!r}")

        payload = response.payload or {}
        status = payload.get("status")
        issues = [
            f"{issue.get('severity')} {issue.get('code')}: {issue.get('message')}"
            for issue in payload.get("issues") or []
        ]
        if status != "ACCEPTED":
            raise InvalidPayloadError(f"patchListingsItem sku={sku!r} returned {status}: {issues}")
        if issues:
            self.logger.warning("patchListingsItem sku=%s accepted with issues: %s", sku, issues)
        return payload
