from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    kafka_bootstrap_servers: str
    kafka_topic: str = "shop.public.orders"
    kafka_group_id: str = "fulfillment-worker"
    # SASL_SSL (TLS + SCRAM-SHA-512) on the cluster; PLAINTEXT only for local tests against a throwaway broker.
    kafka_security_protocol: Literal["SASL_SSL", "PLAINTEXT"] = "SASL_SSL"
    kafka_username: str | None = None
    kafka_password: SecretStr | None = None
    kafka_ca_file: str | None = None  # Strimzi cluster CA (ca.crt), mounted from the copied Secret
    # In Kubernetes: Secret `shop-db-fulfillment-worker` (role fulfillment_worker: INSERT on shipments only).
    database_url: SecretStr
    # Simulated carrier call per new shipment, so consumer lag (and KEDA scaling) can be produced at k6 rates.
    shipment_latency_ms: int = Field(default=20, ge=0, le=10_000)
    batch_max_records: int = Field(default=500, ge=1, le=5_000)
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _sasl_needs_credentials(self) -> "Settings":
        if self.kafka_security_protocol == "SASL_SSL" and not (
            self.kafka_username and self.kafka_password and self.kafka_ca_file
        ):
            raise ValueError("SASL_SSL needs KAFKA_USERNAME, KAFKA_PASSWORD and KAFKA_CA_FILE")
        return self
