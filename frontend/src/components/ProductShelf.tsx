import type { CSSProperties } from 'react';
import type { Title } from '../types/title';
import { productImage, type Product } from '../lib/products';

interface Props {
  item: Title;
  products: Product[];
  onPick: (p: Product) => void;
}

/**
 * The shop shelf. Takes the Detail block's slot under the poster row when the viewer asks
 * about something they saw, and only then — the agent never pushes it. A TV cannot take a
 * card number, so the one action is handing the item to the viewer's phone.
 */
export function ProductShelf({ item, products, onPick }: Props) {
  return (
    <div className="shelf" role="region" aria-label={`Shop ${item.title}`}>
      <div className="shelfhead">
        <h3>Seen in {item.title}</h3>
        <span className="shelfhint">Select one to send it to your phone. Back closes the shelf.</span>
      </div>
      <div className="shelfrow">
        {products.map((p, i) => (
          <button
            key={p.id}
            className="product f"
            style={{ '--i': i } as CSSProperties}
            aria-label={`${p.name} by ${p.brand}, ${p.price}. ${p.note}`}
            onClick={() => onPick(p)}
          >
            <div className="simg"><img src={productImage(p)} alt="" /></div>
            <div className="sbody">
              <span className="sname">{p.name}</span>
              <span className="sbrand">{p.brand}</span>
              <b className="sprice">{p.price}</b>
              <span className="snote">{p.note}</span>
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}
