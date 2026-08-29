from collections.abc import Awaitable, Callable
from typing import Final, Protocol

from .site_icons import SiteIcon

TITLE_FETCHER_STATE: Final = "title_fetcher"
SITE_ICON_SERVICE_STATE: Final = "site_icon_service"
type TitleFetcher = Callable[[str], Awaitable[str | None]]


class IconService(Protocol):
    async def get(self, url: str) -> SiteIcon | None: ...

    async def refresh(self, url: str) -> bool: ...

    async def wait_for_idle(self) -> None: ...
