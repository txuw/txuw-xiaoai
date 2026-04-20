from __future__ import annotations

import inspect
from dataclasses import dataclass
from time import monotonic
from typing import Literal

import httpx
from pydantic import BaseModel


REQUEST_IP_CACHE_KEY = "__request_ip__"


class RegionLookupError(RuntimeError):
    """地区查询失败时抛出的统一异常。"""


class RegionLookupResult(BaseModel):
    """统一的高德地区查询结果模型。"""

    provider: Literal["amap_ip"]
    province: str
    city: str | None = None
    adcode: str | None = None
    rectangle: str | None = None


@dataclass(slots=True, frozen=True)
class _CacheEntry:
    """保存地区缓存命中所需的最小信息。"""

    expires_at: float
    result: RegionLookupResult


class AmapIpRegionProvider:
    """封装高德 Web 服务 IP 定位接口。"""

    _endpoint = "https://restapi.amap.com/v3/ip"

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        key: str,
    ) -> None:
        self._client = client
        self._key = key

    @property
    def key(self) -> str:
        return self._key

    async def lookup(self, ip: str | None = None) -> RegionLookupResult:
        params: dict[str, str] = {"key": self._key}
        normalized_ip = ip.strip() if isinstance(ip, str) else ""
        if normalized_ip:
            params["ip"] = normalized_ip

        response = await self._client.get(self._endpoint, params=params)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RegionLookupError("高德地区接口返回了无法识别的响应。")

        status = payload.get("status")
        if not _is_amap_success(status):
            info = _normalize_text(payload.get("info"))
            infocode = _normalize_text(payload.get("infocode"))
            message = info or "高德地区接口返回失败。"
            if infocode:
                message = f"{message} infocode={infocode}"
            raise RegionLookupError(message)

        province = _normalize_text(payload.get("province"))
        city = _optional_amap_city(payload.get("city"))
        adcode = _optional_text(payload.get("adcode"))
        rectangle = _optional_text(payload.get("rectangle"))

        # 高德 IP 定位只返回行政区范围 rectangle，不返回精确坐标。
        # v1 直接透传官方字段，避免根据 rectangle 人为推导中心点造成伪精度。
        return RegionLookupResult(
            provider="amap_ip",
            province=province,
            city=city,
            adcode=adcode,
            rectangle=rectangle,
        )

    async def close(self) -> None:
        close = getattr(self._client, "aclose", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result


class RegionLookupService:
    """在 provider 之上补充缓存与统一调用语义。"""

    def __init__(
        self,
        provider: AmapIpRegionProvider,
        *,
        cache_ttl_seconds: float = 300.0,
    ) -> None:
        self._provider = provider
        self._cache_ttl_seconds = max(cache_ttl_seconds, 0.0)
        self._cache: dict[str, _CacheEntry] = {}

    @property
    def provider(self) -> AmapIpRegionProvider:
        return self._provider

    async def lookup(self, ip: str | None = None) -> RegionLookupResult:
        cache_key = self._cache_key(ip)
        cached = self._cache.get(cache_key)
        now = monotonic()
        if cached is not None and cached.expires_at >= now:
            return cached.result
        if cached is not None and cached.expires_at < now:
            self._cache.pop(cache_key, None)

        result = await self._provider.lookup(ip=ip)
        if self._cache_ttl_seconds > 0:
            self._cache[cache_key] = _CacheEntry(
                expires_at=now + self._cache_ttl_seconds,
                result=result,
            )
        return result

    async def close(self) -> None:
        await self._provider.close()

    @staticmethod
    def _cache_key(ip: str | None) -> str:
        normalized_ip = ip.strip() if isinstance(ip, str) else ""
        if normalized_ip:
            return normalized_ip
        # 当不传 ip 时，高德会按当前 HTTP 请求来源 IP 定位，因此缓存键必须单独保留一个
        # “请求端 IP”哨兵值，避免和显式 IP 查询混在同一条缓存上。
        return REQUEST_IP_CACHE_KEY


def _is_amap_success(status: object) -> bool:
    return str(status).strip() == "1"


def _normalize_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _optional_text(value: object) -> str | None:
    text = _normalize_text(value)
    return text or None


def _optional_amap_city(value: object) -> str | None:
    if isinstance(value, list):
        items = [item.strip() for item in value if isinstance(item, str) and item.strip()]
        if not items:
            return None
        return items[0]
    return _optional_text(value)
