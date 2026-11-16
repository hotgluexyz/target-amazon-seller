"""Amazon Seller target class."""

import json
import os
import sys

from hotglue_singer_sdk import typing as th
from hotglue_singer_sdk.exceptions import ConfigValidationError
from hotglue_singer_sdk.helpers.capabilities import AlertingLevel
from hotglue_singer_sdk.target_sdk.target import TargetHotglue

os.environ.setdefault("ENV_DISABLE_DONATION_MSG", "1")


def _apply_sandbox_env_from_argv() -> None:
    """Set AWS_ENV=SANDBOX before sp_api is imported if the config file requests it.

    sp_api reads AWS_ENV at import time, so this must run before the sinks
    import below. Errors reading the file are ignored here and surface later
    when the SDK validates the config.
    """
    for i, arg in enumerate(sys.argv):
        if arg != "--config" or i + 1 >= len(sys.argv):
            continue
        try:
            with open(sys.argv[i + 1], encoding="utf-8") as config_file:
                config = json.load(config_file)
        except (OSError, json.JSONDecodeError):
            return
        if config.get("sandbox"):
            os.environ["AWS_ENV"] = "SANDBOX"
        return


_apply_sandbox_env_from_argv()

from sp_api.base import marketplaces  # noqa: E402

from target_amazon_seller.sinks import FulfillmentsSink, ProductsSink, UpdateInventorySink  # noqa: E402


class TargetAmazonSeller(TargetHotglue):
    """Singer target for Amazon Seller SP-API writes."""

    name = "target-amazon-seller"
    alerting_level = AlertingLevel.ERROR
    SINK_TYPES = [ProductsSink, UpdateInventorySink, FulfillmentsSink]

    config_jsonschema = th.PropertiesList(
        th.Property("lwa_client_id", th.StringType, required=True),
        th.Property("client_secret", th.StringType, required=True),
        th.Property("refresh_token", th.StringType, required=True),
        th.Property("selling_partner_id", th.StringType, required=True),
        th.Property("aws_access_key", th.StringType, required=False),
        th.Property("aws_secret_key", th.StringType, required=False),
        th.Property("role_arn", th.StringType, required=False),
        th.Property(
            "marketplace",
            th.StringType,
            description="Marketplace country code, for example US",
            required=False,
        ),
        th.Property("sandbox", th.BooleanType, required=False),
    ).to_dict()

    def __init__(
        self,
        config=None,
        parse_env_config: bool = False,
        validate_config: bool = True,
        state: str = None,
    ) -> None:
        super().__init__(
            config=config,
            parse_env_config=parse_env_config,
            validate_config=validate_config,
            state=state,
        )
        if self.config.get("sandbox"):
            if marketplaces.AWS_ENVIRONMENT != marketplaces.AwsEnv.SANDBOX.value:
                raise ConfigValidationError(
                    "Config has sandbox=true but sp_api loaded production endpoints. "
                    "Run with --config <path> so sandbox is applied before sp_api loads, "
                    "or export AWS_ENV=SANDBOX before starting the process."
                )
            self.logger.info("SP-API sandbox mode enabled (AWS_ENV=SANDBOX)")


if __name__ == "__main__":
    TargetAmazonSeller.cli()
