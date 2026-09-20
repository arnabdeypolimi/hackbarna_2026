// The shop: three products per title, keyed by the backend's title_id (str(tmdb_id)).
// The backend owns the catalogue (GET /shop) so the agent can name an item while the TV
// slides it in; the whole map is held here because `show_products` opens the shelf on the
// ack, not after a round trip. Only the photos are TV assets.
import type { Title } from '../types/title';
import { toWireId } from './tvBridge';

export interface Product {
  id: string;
  name: string;
  brand: string;
  /** Preformatted — the shop decides currency and rounding, the TV only shows it. */
  price: string;
  /** Where in the film it appears, or what the item is. One line. */
  note: string;
  /** Relative to the app's base URL, like the poster paths. */
  image: string;
}

export type ProductMap = Record<string, Product[]>;

// Same origin like /config and /sessions: the Vite proxy in dev, the OS's reverse proxy on a set.
export const SHOP_URL = '/shop';

export async function loadProducts(): Promise<ProductMap> {
  const r = await fetch(SHOP_URL);
  // No backend is "no shop": the shelf is optional, browsing is not.
  if (!r.ok) return {};
  const body = (await r.json()) as { products?: ProductMap };
  return body.products ?? {};
}

export function productsFor(map: ProductMap, title: Title): Product[] {
  return map[toWireId(title)] ?? [];
}

export const productImage = (p: Product): string => `${import.meta.env.BASE_URL}${p.image}`;
