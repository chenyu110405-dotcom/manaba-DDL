"""Public configuration; contains no passwords, cookies or account identifiers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from urllib.parse import urlsplit


DEFAULT_URL = "https://manaba.ryukoku.ac.jp/ct/home"
MANABA_HOST = "manaba.ryukoku.ac.jp"
TIMEZONE = "Asia/Tokyo"


class ConfigError(ValueError):
    """An actionable, non-sensitive configuration error."""


def normalize_home_url(value: str) -> str:
    """Accept only this adapter's supported campus and canonicalize its home."""
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("请填写龙谷大学 manaba 的 HTTPS 网址。")
    candidate = value.strip()
    try:
        parts = urlsplit(candidate)
        invalid_authority = (
            parts.hostname != MANABA_HOST
            or parts.username is not None
            or parts.password is not None
            or parts.port is not None
            or parts.netloc.lower() != MANABA_HOST
        )
    except ValueError:
        raise ConfigError("网址格式无效。") from None
    valid_path = parts.path in {"", "/", "/ct", "/ct/", "/ct/home", "/ct/home_course"}
    valid_path = valid_path or (
        parts.path.startswith("/ct/home_library_query")
        and set(parts.path.removeprefix("/ct/home_library_query")) <= {"_"}
    )
    if parts.scheme != "https" or invalid_authority or not valid_path or parts.query or parts.fragment:
        raise ConfigError("目前仅支持龙谷大学 manaba 首页网址；其他校园平台需要单独适配。")
    return DEFAULT_URL


def _number(value: object, label: str, minimum: float, maximum: float | None = None) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{label}必须是数字。")
    if not math.isfinite(value) or value < minimum or (maximum is not None and value > maximum):
        limit = f"{minimum:g} 至 {maximum:g}" if maximum is not None else f"至少 {minimum:g}"
        raise ConfigError(f"{label}必须为{limit}。")


@dataclass(frozen=True, slots=True)
class ScraperConfig:
    """Settings for one user's fresh browser session. Synchronization defaults to 10 minutes."""

    url: str = DEFAULT_URL
    interval_seconds: float = 600
    timeout_seconds: float = 30
    throttle_seconds: float = 0.75
    max_pages: int = 250
    max_courses: int = 80
    headless: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "url", normalize_home_url(self.url))
        _number(self.interval_seconds, "同步间隔（秒）", 60)
        _number(self.timeout_seconds, "请求超时（秒）", 1, 300)
        _number(self.throttle_seconds, "请求间隔（秒）", 0.25)
        for label, value in (("最大页数", self.max_pages), ("最大课程数", self.max_courses)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ConfigError(f"{label}必须是正整数。")
        if not isinstance(self.headless, bool):
            raise ConfigError("headless 必须为布尔值。")

    @property
    def origin(self) -> str:
        return f"https://{MANABA_HOST}"

    @property
    def timezone(self) -> str:
        return TIMEZONE

    def as_dict(self) -> dict[str, object]:
        return asdict(self)
