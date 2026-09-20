"""The shop catalogue: what the TV can sell against each title, from ``products.json``.

Mocked and static for now — a commerce backend would replace ``load_shop`` and nothing
else. Keyed by title_id (str(tmdb_id), D11) like everything else that crosses to the TV.
The backend owns it so the agent can *speak* an item ("that's the pink satin bomber,
89 euros") while the TV slides it in; the TV fetches the same catalogue from ``/shop``
so the two can never disagree about what is on the shelf.
"""
import json
import os
from functools import lru_cache
from pathlib import Path

from loguru import logger
from pydantic import BaseModel, Field, RootModel, ValidationError

DEFAULT_SHOP_PATH = Path(__file__).resolve().parents[2] / "products.json"
SHOP_PATH_ENV = "PRODUCTS_FILE"


class Product(BaseModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    brand: str
    #: Preformatted — the shop decides currency and rounding, nobody else reformats it.
    price: str
    #: Where in the film it appears, or what the item is. One line.
    note: str = ""
    #: Asset path relative to the TV app's base URL. The photos ship with the TV, not
    #: with this backend: they are UI assets, sized and cached for the set.
    image: str


class ShopCatalog(RootModel[dict[str, list[Product]]]):
    def products_for(self, title_id: str) -> list[Product]:
        return self.root.get(title_id, [])

    def has(self, title_id: str) -> bool:
        return bool(self.root.get(title_id))

    def render_for_prompt(self, title_id: str) -> str:
        """The inline shelf summary for a Screen tile: names and prices only. The
        model needs to recognise "that jacket", not read a product page."""
        return "; ".join(f"{p.name} {p.price}" for p in self.products_for(title_id))

    def public(self) -> dict:
        return {"products": {k: [p.model_dump() for p in v] for k, v in self.root.items()}}


def load_shop(path: Path) -> ShopCatalog:
    """Missing file = an empty shop, logged — the shelf is optional, unlike avatars.yaml.
    A file that exists but is wrong is a bug and fails loudly."""
    if not path.exists():
        logger.warning("shop catalogue not found at {}; show_products will have nothing to show", path)
        return ShopCatalog({})
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        raise ValueError(f"shop catalogue {path} is not valid JSON: {err}") from err
    try:
        return ShopCatalog.model_validate(raw)
    except ValidationError as err:
        raise ValueError(f"shop catalogue {path} is malformed: {err}") from err


def shop_path() -> Path:
    # Like the avatar catalog: read directly, not via Settings, so it loads without keys.
    return Path(os.environ.get(SHOP_PATH_ENV) or DEFAULT_SHOP_PATH)


@lru_cache
def get_shop() -> ShopCatalog:
    return load_shop(shop_path())
