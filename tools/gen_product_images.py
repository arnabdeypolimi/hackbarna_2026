"""Render the mock shop's product photos with fal.ai (FLUX schnell).

Reads products.json (repo root, served by the backend at /shop) and writes one JPEG per product
under frontend/public/products/<title_id>/. Idempotent: existing files are
skipped, so a failed run can be resumed. FAL_KEY comes from the environment
or .env and is never printed.

    uv run python tools/gen_product_images.py            # everything missing
    uv run python tools/gen_product_images.py --force    # redo all
    uv run python tools/gen_product_images.py --only barbie-bomber wick-coin
"""
import argparse
import json
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv
from loguru import logger

ROOT = Path(__file__).resolve().parents[1]
PRODUCTS = ROOT / "products.json"
OUT = ROOT / "frontend/public"

#: Synchronous endpoint — schnell answers in a few seconds, no need for the queue API.
FAL_URL = "https://fal.run/fal-ai/flux/schnell"

#: Every product is one item, catalogue-style. The suffix keeps them looking like one
#: shop; the negatives keep the model off text and faces, which it renders badly and
#: which would read as counterfeit branding on a TV.
_SUFFIX = (
    ", e-commerce product photography, single item centred, soft studio lighting, "
    "clean warm off-white seamless background, shallow depth of field, 85mm lens, "
    "high detail. No text, no logos, no people, no hands, no watermark."
)

PROMPTS: dict[str, str] = {
    "barbie-bomber": "hot pink satin bomber jacket with white ribbed cuffs and collar, on an invisible mannequin",
    "barbie-skates": "pair of retro neon pink and yellow quad roller skates with pastel wheels",
    "barbie-hat": "bubblegum pink felt western cowboy hat with a white braided band",
    "oppenheimer-fedora": "wide-brim brown wool fedora hat, 1940s style, grosgrain ribbon band",
    "oppenheimer-glasses": "vintage round tortoiseshell sunglasses with dark green lenses, folded",
    "oppenheimer-print": "framed art print of a desert horizon at dawn with a steel tower silhouette, thin black frame, minimal",
    "wick-suit": "tailored black wool suit jacket with a slim lapel, on an invisible mannequin, crisp white shirt underneath",
    "wick-gloves": "pair of black leather driving gloves, fitted, laid flat",
    "wick-coin": "antique gold coin with an engraved crest, resting in an open black velvet box",
    "spiderverse-hoodie": "red and black colour-blocked hoodie with spray-paint graffiti splatter texture, on an invisible mannequin",
    "spiderverse-sneakers": "pair of red canvas high-top sneakers with white laces untied, one propped on the other",
    "spiderverse-bottle": "insulated stainless steel water bottle shaped like a graffiti spray paint can, red cap, matte black body",
    "mario-cap": "bright red cotton baseball cap with a round white embroidered patch on the front",
    "mario-lamp": "glossy golden-yellow cube desk lamp with a raised question mark shape, softly glowing",
    "mario-plush": "round red plush toy mushroom with white spots and a cream face, soft fabric",
    "gt-jacket": "red white and black motorsport racing team jacket with striped sleeves, on an invisible mannequin",
    "gt-gloves": "pair of red and black motorsport racing gloves with suede palms and a velcro cuff",
    "gt-model": "die-cast model of a silver sports race car with racing stripes on a small black display stand",
}


def render(client: httpx.Client, key: str, prompt: str) -> bytes:
    r = client.post(
        FAL_URL,
        headers={"Authorization": f"Key {key}"},
        json={
            "prompt": prompt + _SUFFIX,
            # 512px: the card shows it at ~300px and the app targets 1–1.5 GB TVs.
            "image_size": "square",
            "num_images": 1,
            "output_format": "jpeg",
            "enable_safety_checker": True,
        },
        timeout=120,
    )
    r.raise_for_status()
    url = r.json()["images"][0]["url"]
    return client.get(url, timeout=120).content


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--only", nargs="*", default=None)
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    key = os.environ.get("FAL_KEY")
    if not key:
        raise SystemExit("FAL_KEY is not set (environment or .env)")

    products = json.loads(PRODUCTS.read_text(encoding="utf-8"))
    todo = [p for group in products.values() for p in group if not args.only or p["id"] in args.only]
    missing = [p["id"] for p in todo if p["id"] not in PROMPTS]
    if missing:
        raise SystemExit(f"no prompt for: {missing}")

    with httpx.Client() as client:
        for p in todo:
            dest = OUT / p["image"]
            if dest.exists() and not args.force:
                logger.info("skip {}", p["id"])
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(render(client, key, PROMPTS[p["id"]]))
            logger.info("wrote {} ({} KB)", dest.relative_to(ROOT), dest.stat().st_size // 1024)


if __name__ == "__main__":
    main()
