// The mock shop: three products per title, keyed by the backend's title_id (str(tmdb_id)),
// served as a static file next to the dataset. A real integration would swap the fetch for
// the OS's commerce API; nothing else here would move.
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

export const PRODUCTS_URL = `${import.meta.env.BASE_URL}data/products.json`;

export async function loadProducts(): Promise<ProductMap> {
  const r = await fetch(PRODUCTS_URL);
  if (!r.ok) return {};
  const text = await r.text();
  // Dev servers answer unknown paths with index.html; that is "no shop", not an error.
  if (text.trimStart().startsWith('<')) return {};
  return JSON.parse(text) as ProductMap;
}

export function productsFor(map: ProductMap, title: Title): Product[] {
  return map[toWireId(title)] ?? [];
}

export const productImage = (p: Product): string => `${import.meta.env.BASE_URL}${p.image}`;
