from pydantic import SecretStr
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # In Kubernetes: Secret `shop-db-app` key `uri` (postgresql://...), see docs/contracts/services.md.
    database_url: SecretStr
    payments_url: str = "http://localhost:8002"
    log_level: str = "INFO"
