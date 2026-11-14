"""Request / response models."""
import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, validator

from config import DEFAULT_FB_URL, MAX_INGEST_BATCH, STATES, STATES_AND_ALL


# --------------------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------------------
class SmartPasteInput(BaseModel):
    raw_text: str = Field(..., max_length=8000)
    source: str = "Facebook Marketplace"
    url: str = DEFAULT_FB_URL
    save_to_feed: bool = True


class QuickRegionUpdate(BaseModel):
    state: str

    @validator("state")
    def _state(cls, v: str) -> str:
        v = v.upper()
        if v not in STATES_AND_ALL:
            raise ValueError("Unknown state")
        return v


class LiveScanRequest(BaseModel):
    custom_query: Optional[str] = Field(None, max_length=80)
    sources: List[str] = ["facebook", "gumtree"]
    full_fb_sweep: bool = False

    @validator("sources", each_item=True)
    def _src(cls, v: str) -> str:
        if v not in ("facebook", "gumtree"):
            raise ValueError("Unknown source")
        return v


class DeepScanRequest(BaseModel):
    include_gumtree: bool = True
    max_pages_per_query: int = Field(25, ge=1, le=200)


class StatusUpdateInput(BaseModel):
    status: str

    @validator("status")
    def _status(cls, v: str) -> str:
        if v not in ("NEW", "SAVED", "HIDDEN"):
            raise ValueError("Invalid status")
        return v


class ReferencePriceUpdate(BaseModel):
    avg_used_aud: float = Field(..., gt=0, lt=100000)
    good_deal_aud: float = Field(..., gt=0, lt=100000)
    steal_aud: float = Field(..., gt=0, lt=100000)

    @validator("steal_aud")
    def _order(cls, v: float, values: Dict[str, Any]) -> float:
        good, avg = values.get("good_deal_aud"), values.get("avg_used_aud")
        if good is not None and v > good:
            raise ValueError("Steal price must be at or below Good Deal price")
        if good is not None and avg is not None and good > avg:
            raise ValueError("Good Deal price must be at or below the average")
        return v


class SettingsUpdate(BaseModel):
    state: str
    city: str = Field(..., max_length=60)
    radius_km: int = Field(50, ge=1, le=500)
    min_discount_pct: float = Field(20, ge=0, le=100)
    min_profit_aud: float = Field(50, ge=0, le=100000)
    auto_notify_tier: str = "GOOD_DEAL"
    # None = leave the stored secret untouched; "" = clear it; anything else = replace it.
    discord_webhook_url: Optional[str] = None
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = Field(None, max_length=40)
    search_queries: List[str] = Field(..., max_items=60)
    exclude_keywords: List[str] = Field(..., max_items=200)
    # Engine / routing / alert extras (all optional: omitted = unchanged)
    home_suburb: Optional[str] = Field(None, max_length=60)
    min_roi_pct: Optional[float] = Field(None, ge=0, le=1000)
    hourly_value_aud: Optional[float] = Field(None, ge=0, le=500)
    cost_per_km_aud: Optional[float] = Field(None, ge=0, le=5)
    gst_registered: Optional[bool] = None
    mac_notify: Optional[bool] = None
    scheduler_enabled: Optional[bool] = None
    ntfy_topic: Optional[str] = Field(None, max_length=64)

    @validator("ntfy_topic")
    def _ntfy(cls, v: Optional[str]) -> Optional[str]:
        if v and not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", v):
            raise ValueError("ntfy topic must be 8-64 letters, digits, _ or -")
        return v

    @validator("state")
    def _state(cls, v: str) -> str:
        v = v.upper()
        if v not in STATES:
            raise ValueError("Unknown state")
        return v

    @validator("auto_notify_tier")
    def _tier(cls, v: str) -> str:
        if v not in ("OFF", "STEAL", "GOOD_DEAL"):
            raise ValueError("Invalid alert tier")
        return v

    @validator("discord_webhook_url")
    def _discord(cls, v: Optional[str]) -> Optional[str]:
        # Only real Discord webhooks: otherwise this endpoint is a way to make the server POST anywhere.
        if v and not re.fullmatch(r"https://(?:canary\.|ptb\.)?(?:discord|discordapp)\.com/api/webhooks/\d+/[\w-]+", v):
            raise ValueError("Not a valid Discord webhook URL")
        return v

    @validator("telegram_bot_token")
    def _tg(cls, v: Optional[str]) -> Optional[str]:
        if v and not re.fullmatch(r"\d{5,15}:[A-Za-z0-9_-]{30,60}", v):
            raise ValueError("Not a valid Telegram bot token")
        return v

    @validator("search_queries", "exclude_keywords", each_item=True)
    def _short(cls, v: str) -> str:
        return v.strip()[:80]


class IngestBatchPayload(BaseModel):
    listings: List[Dict[str, Any]] = Field(..., max_items=MAX_INGEST_BATCH)


class PriceUpdate(BaseModel):
    part_id: Optional[int] = None
    canonical_name: Optional[str] = Field(None, max_length=120)
    new_retail_aud: float = Field(..., ge=0, le=100000)


class PricesPayload(BaseModel):
    prices: List[PriceUpdate] = Field(..., max_items=500)


