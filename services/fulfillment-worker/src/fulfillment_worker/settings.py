from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings
from sqlalchemy.engine import URL

# A batch's simulated latency must stay well below the liveness stall bound (main.STALLED_AFTER_S = 120s) and the
# consumer's max.poll.interval (300s): half the stall bound, so DB retries (~25s) still fit.
MAX_BATCH_LATENCY_MS = 60_000


class Settings(BaseSettings):
    kafka_bootstrap_servers: str
    kafka_topic: str = "shop.public.orders"
    kafka_group_id: str = "fulfillment-worker"
    # SASL_SSL (TLS + SCRAM-SHA-512) on the cluster; PLAINTEXT only for local tests against a throwaway broker.
    kafka_security_protocol: Literal["SASL_SSL", "PLAINTEXT"] = "SASL_SSL"
    kafka_username: str | None = None
    kafka_password: SecretStr | None = None
    kafka_ca_file: str | None = None  # Strimzi cluster CA (ca.crt), mounted from the copied Secret

    # Either a full DATABASE_URL (compose, tests) or its parts: the CNPG managed-role Secret
    # `shop-db-fulfillment-worker` has only `username` and `password`; building the URL here percent-encodes them.
    database_url: SecretStr | None = None
    database_host: str | None = None
    database_port: int = 5432
    database_name: str = "shop"
    database_user: str | None = None
    database_password: SecretStr | None = None
    database_sslmode: Literal["disable", "prefer", "require", "verify-ca", "verify-full"] | None = None

    # Simulated carrier call per new shipment, so consumer lag (and KEDA scaling) can be produced at k6 rates.
    shipment_latency_ms: int = Field(default=20, ge=0, le=10_000)
    batch_max_records: int = Field(default=500, ge=1, le=5_000)
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _complete(self) -> "Settings":
        if self.kafka_security_protocol == "SASL_SSL" and not (
            self.kafka_username and self.kafka_password and self.kafka_ca_file
        ):
            raise ValueError("SASL_SSL needs KAFKA_USERNAME, KAFKA_PASSWORD and KAFKA_CA_FILE")
        if not self.database_url and not (self.database_host and self.database_user and self.database_password):
            raise ValueError("set DATABASE_URL, or DATABASE_HOST, DATABASE_USER and DATABASE_PASSWORD")
        batch_latency_ms = self.shipment_latency_ms * self.batch_max_records
        if batch_latency_ms > MAX_BATCH_LATENCY_MS:
            largest_batch = MAX_BATCH_LATENCY_MS // self.shipment_latency_ms
            raise ValueError(
                f"SHIPMENT_LATENCY_MS x BATCH_MAX_RECORDS = {batch_latency_ms}ms exceeds {MAX_BATCH_LATENCY_MS}ms: a"
                f" batch would outlast the liveness stall bound; set BATCH_MAX_RECORDS <= {largest_batch}"
            )
        return self

    def dsn(self) -> str:
        if self.database_url:
            return self.database_url.get_secret_value()
        password = self.database_password.get_secret_value() if self.database_password else None
        url = URL.create(
            "postgresql",
            username=self.database_user,
            password=password,
            host=self.database_host,
            port=self.database_port,
            database=self.database_name,
            query={"sslmode": self.database_sslmode} if self.database_sslmode else {},
        )
        return url.render_as_string(hide_password=False)  # async_dsn() maps it to asyncpg (sslmode -> ssl)
