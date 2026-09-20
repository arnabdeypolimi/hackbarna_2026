import json

import pytest
from fastapi.testclient import TestClient

from conftest import PERSONA
from tv_avatar.app import create_app
from tv_avatar.control.protocol import Playback, ScreenState, Tile
from tv_avatar.session.state import SessionStore
from tv_avatar.shop import ShopCatalog, get_shop, load_shop

SHELF = {"346698": [
    {"id": "barbie-bomber", "name": "Pink Satin Bomber Jacket", "brand": "Dreamhouse Apparel",
     "price": "€89", "note": "The jacket from the roller-skating scene", "image": "products/346698/barbie-bomber.jpg"},
]}


def test_repo_catalogue_loads_and_has_three_products_per_title():
    shop = get_shop()
    assert shop.root, "products.json at the repo root must load"
    assert all(len(v) == 3 for v in shop.root.values())


def test_missing_file_is_an_empty_shop_not_a_crash(tmp_path):
    assert load_shop(tmp_path / "nope.json").root == {}


def test_malformed_file_fails_loudly(tmp_path):
    bad = tmp_path / "products.json"
    bad.write_text(json.dumps({"1": [{"id": "x"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="malformed"):
        load_shop(bad)
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_shop(bad)


def test_prompt_summary_is_names_and_prices_only():
    shop = ShopCatalog.model_validate(SHELF)
    assert shop.render_for_prompt("346698") == "Pink Satin Bomber Jacket €89"
    assert shop.render_for_prompt("000") == ""


def test_screen_render_names_the_shelf_items_for_shoppable_tiles(monkeypatch):
    monkeypatch.setattr("tv_avatar.session.state.get_shop", lambda: ShopCatalog.model_validate(SHELF))
    s = SessionStore().create(60, PERSONA)
    s.update_screen(ScreenState(
        view="grid", focus_index=0,
        tiles=[Tile(title_id="346698", name="Barbie", position=0, shoppable=True),
               Tile(title_id="999", name="Unlisted", position=1, shoppable=True),
               Tile(title_id="565770", name="Blue Beetle", position=2)],
        playback=Playback(state="stopped"),
    ))
    barbie, unlisted, beetle = [ln for ln in s.render_for_prompt().splitlines() if ln.startswith("  [")]
    assert "[shop: Pink Satin Bomber Jacket €89]" in barbie
    # The TV can show a shelf the backend knows nothing about: still marked, just unnamed.
    assert unlisted.endswith("(id=999) [shop]")
    assert "[shop" not in beetle


def test_shoppable_tile_over_the_socket_reaches_the_prompt_with_its_items():
    """End to end: what the TV pushes on the control socket is what the model reads."""
    app = create_app()
    with TestClient(app) as c:
        body = c.post("/sessions").json()
        url = f"/sessions/{body['session_id']}/control?token={body['control_token']}"
        with c.websocket_connect(url) as ws:
            ws.receive_json()  # agent_status idle
            ws.send_text(json.dumps({
                "v": 1, "type": "screen_state",
                "state": {"view": "grid", "focus_index": 0,
                          "tiles": [{"title_id": "346698", "name": "Barbie", "position": 0, "shoppable": True}],
                          "playback": {"state": "stopped"}},
            }))
            ws.send_text(json.dumps({"v": 99}))
            assert ws.receive_json()["code"] == "unsupported_version"  # the reader has drained
        rendered = app.state.store.get(body["session_id"]).render_for_prompt()
    assert "Barbie (id=346698) [shop: Pink Satin Bomber Jacket €89; Neon Roller Skates €129; Pink Western Hat €39] <- focused" in rendered


def test_shop_endpoints_serve_the_catalogue():
    with TestClient(create_app()) as c:
        body = c.get("/shop").json()
        assert body["products"] == get_shop().public()["products"]
        title_id = next(iter(body["products"]))
        one = c.get(f"/shop/{title_id}").json()
        assert one["title_id"] == title_id and len(one["products"]) == 3
        assert c.get("/shop/not-a-title").status_code == 404
