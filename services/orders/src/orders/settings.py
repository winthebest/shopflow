from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings

from orders.resilience import BreakerConfig, RetryPolicy


class Settings(BaseSettings):
    # In Kubernetes: Secret `shop-db-app` key `uri` (postgresql://...), see docs/contracts/services.md.
    database_url: SecretStr
    payments_url: str = "http://localhost:8002"
    log_level: str = "INFO"
    # Retry and circuit breaker for payments (ADR 0102); the 800ms overall deadline is a contract, not a setting.
    payments_attempts: int = Field(default=3, ge=1, le=5)
    # Per attempt, and what lets a second attempt fit in the 800ms: after a 500ms timeout ~250ms remain for a retry
    # on a new connection (healthy payments answers in ~50ms). Lower buys more retries but turns a payments that is
    # uniformly slower than this, yet under 800ms (today a slow success), into 100% failures and an open circuit.
    payments_attempt_timeout_ms: int = Field(default=500, ge=50, le=800)
    payments_breaker_window_s: float = Field(default=10.0, gt=0)
    payments_breaker_min_calls: int = Field(default=20, ge=1)
    payments_breaker_failure_ratio: float = Field(default=0.75, gt=0, le=1)
    payments_breaker_open_s: float = Field(default=5.0, gt=0)

    def retry_policy(self) -> RetryPolicy:
        return RetryPolicy(attempts=self.payments_attempts, attempt_timeout_s=self.payments_attempt_timeout_ms / 1000)

    def breaker_config(self) -> BreakerConfig:
        return BreakerConfig(
            window_s=self.payments_breaker_window_s,
            min_calls=self.payments_breaker_min_calls,
            failure_ratio=self.payments_breaker_failure_ratio,
            open_s=self.payments_breaker_open_s,
        )
