# target-amazon-seller

Singer target for the [Amazon Selling Partner API](https://developer-docs.amazon.com/sp-api/). It updates listing prices, updates seller-fulfilled (MFN) inventory and confirms order shipments. Built with the [Hotglue Singer SDK](https://github.com/hotgluexyz/HotglueSingerSDK).

## Installation

```bash
pip install target-amazon-seller
```

From source:

```bash
pip install -e .
```

## Configuration

| Field | Required | Description |
|-------|----------|-------------|
| `lwa_client_id` | Yes | LWA application client id |
| `client_secret` | Yes | LWA client secret |
| `refresh_token` | Yes | Seller refresh token |
| `selling_partner_id` | Yes | Merchant id from OAuth (stored on connection) |
| `aws_access_key` | No | IAM user access key for SP-API signing |
| `aws_secret_key` | No | IAM secret key |
| `role_arn` | No | IAM role ARN to assume |
| `marketplace` | No | Marketplace country code (default `US`) |
| `sandbox` | No | Use the SP-API sandbox host when `true` |

## Identifiers

Amazon's listing write APIs only accept the **seller SKU**. `Products` and `UpdateInventory` find the listing by `sku`, or by the variant id when `sku` is missing.

This matches the unified read for Amazon Seller:

| Unified read | Amazon value | Used by this target |
|--------------|--------------|---------------------|
| `Product.id` | ASIN (parent ASIN for variation families) | No |
| `Product.variants[].id` / `variants[].sku` | Seller SKU | Yes, as `variants[].id` / `variants[].sku` / `variant_id` / `sku` |
| `SalesOrder.id` | Amazon order id | Yes, as `order_id` on `Fulfillments` |
| `SalesOrder.line_items[].id` | Amazon OrderItemId | Yes, as `line_items[].id` on `Fulfillments` |

The ASIN identifies Amazon's catalog item, not the seller's listing. One ASIN can have several SKUs (for example an FBA and an FBM offer), so it can't be used to pick a listing. Variation parents are not buyable and have no price or inventory, so updates must target child SKUs.

## Streams

### Products

Updates the price of existing listings. It does not create, delete or change images.

```json
{
  "variants": [
    { "sku": "MY-SKU-001", "price": "29.99" },
    { "id": "MY-SKU-002", "price": "19.99" }
  ]
}
```

A record without `variants` is treated as a single variant:

```json
{ "sku": "MY-SKU-001", "price": "29.99" }
```

| Field | Required | Notes |
|-------|----------|-------|
| `variants[].sku` or `variants[].id` | Yes | Seller SKU |
| `variants[].price` | Yes | Number or numeric string; the currency comes from the listing's current offer |

Variants without a SKU or price are skipped with a warning. The record fails if no variant can be updated.

### UpdateInventory

Updates MFN quantity and handling time on an existing listing.

```json
{
  "sku": "MY-SKU-001",
  "operation": "set",
  "quantity": 25,
  "lead_time_to_ship_max_days": 2
}
```

| Field | Required | Notes |
|-------|----------|-------|
| `sku` or `variant_id` | Yes | Seller SKU |
| `quantity` | Yes | Integer |
| `operation` | No | `set` (default), `add` or `subtract`; `add` and `subtract` apply to the listing's current MFN quantity |
| `lead_time_to_ship_max_days` | No | Handling time in days; when omitted, the listing's current value is kept |
| `switch_to_mfn` | No | Must be `true` to update a listing fulfilled by Amazon (FBA), which converts it to seller-fulfilled |

### Fulfillments

Confirms one shipped package per record. Partial shipments are sent as separate records with their own `line_items`.

Partial shipment:

```json
{
  "order_id": "902-1106328-1059050",
  "carrier": "FedEx",
  "shipping_method": "FedEx Ground",
  "tracking_number": "112345678",
  "ship_date": "2026-11-13T15:00:00Z",
  "line_items": [
    { "id": "79039765272157", "quantity": 1 }
  ]
}
```

Whole order (ships every unit not yet shipped):

```json
{
  "order_id": "902-1106328-1059050",
  "carrier": "USPS",
  "tracking_number": "TRACK123456789"
}
```

| Field | Required | Notes |
|-------|----------|-------|
| `order_id` | Yes | Amazon order id |
| `carrier` | Yes | Amazon carrier code, for example `UPS`, `USPS`, `FedEx` or `Other` |
| `carrier_name` | When `carrier` is `Other` | Carrier name |
| `tracking_number` | Yes | |
| `shipping_method` | No | Defaults to `Standard` |
| `ship_date` | No | ISO 8601; defaults to the current UTC time |
| `line_items` | No | Omit to ship all unshipped units on the order |
| `line_items[].id` | Yes, per line | Amazon OrderItemId (not the SKU) |
| `line_items[].quantity` | Yes, per line | Positive integer |

The target assigns each package a numeric `packageReferenceId` that is unique within the order, including packages confirmed in earlier runs.

## Errors and retries

Records that fail validation or that Amazon rejects are marked as failed with an `InvalidPayloadError`, and the error names the SKU or order. A listing patch that Amazon answers with status `INVALID` also counts as a failure. Throttled calls (HTTP 429) are retried with a short backoff, up to 5 attempts in total.

## Usage

```bash
cat sample_payload/*.singer | target-amazon-seller --config .secrets/config.json
```

## Development

Requires Python 3.10 through 3.14.

```bash
python -m venv .venv
.venv/bin/pip install -e .
.venv/bin/pip install tox pytest
tox
```

Set `"sandbox": true` in the config to run against the SP-API sandbox. Pass the config as `--config <path>` (or export `AWS_ENV=SANDBOX`), otherwise the target stops instead of calling production. Sandbox responses are static, so only requests that match Amazon's sandbox examples succeed.
