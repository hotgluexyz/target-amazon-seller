"""Tests standard target features using the built-in SDK tests library."""

from hotglue_singer_sdk.testing import get_standard_target_tests

from target_amazon_seller.target import TargetAmazonSeller

SAMPLE_CONFIG = {
    "lwa_client_id": "amzn1.application-oa2-client.fake-client-id",
    "client_secret": "fake-client-secret",
    "refresh_token": "Atzr|fake-refresh-token",
    "selling_partner_id": "A1TESTSELLERID",
    "aws_access_key": "AKIATESTACCESSKEY",
    "aws_secret_key": "fake-secret-key",
    "role_arn": "arn:aws:iam::123456789012:role/fake-sp-api-role",
    "marketplace": "US",
    "sandbox": True,
}


def test_standard_target_tests():
    """Run SDK standard target tests with a minimal fake config."""
    tests = get_standard_target_tests(TargetAmazonSeller, config=SAMPLE_CONFIG)
    for test in tests:
        test()


def test_sink_types_registered():
    names = {sink.name for sink in TargetAmazonSeller.SINK_TYPES}
    assert names == {"Products", "UpdateInventory", "Fulfillments"}


def test_config_requires_selling_partner_id():
    schema = TargetAmazonSeller.config_jsonschema
    required = set(schema.get("required") or [])
    assert "selling_partner_id" in required
