export interface Variant {
  id: number;
  title: string;
  price: number;
  compareAt: number | null;
  available: boolean;
  sku?: string;
  option1?: string | null;
  option2?: string | null;
}

export interface Product {
  id: number;
  handle: string;
  title: string;
  vendor: string;
  productType: string;
  tags: string[];
  price: number;
  compareAt: number | null;
  images: string[];
  variants: Variant[];
  options: { name: string; values: string[] }[];
  descriptionHtml: string;
  createdAt: string;
}

export interface Collection {
  id: number;
  handle: string;
  title: string;
  description: string;
  image: string | null;
  productsCount: number;
}

export interface NavItem {
  title: string;
  url: string;
  children?: NavItem[];
}

export interface Catalog {
  products: Product[];
  collections: Collection[];
  collectionProducts: Record<string, string[]>; // handle -> product handles (merchandised order)
  nav: NavItem[];
  crawledAt: string;
}
