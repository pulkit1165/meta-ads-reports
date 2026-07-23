// Tiny HTML → content-block parser for Shopify product descriptions.
// Splits body_html into sections on headings/bold-leads so the product
// page can render Benefits / Ingredients / How to use as accordions.
export interface DescBlock {
  heading: string | null;
  text: string;
}

const decode = (s: string) =>
  s
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;|&rsquo;/g, "'")
    .replace(/&ldquo;|&rdquo;/g, '"')
    .replace(/&nbsp;/g, ' ')
    .replace(/&#(\d+);/g, (_, n) => String.fromCharCode(parseInt(n, 10)));

function stripTags(html: string): string {
  return decode(
    html
      .replace(/<br\s*\/?>/gi, '\n')
      .replace(/<\/(p|div|li|h[1-6]|tr)>/gi, '\n')
      .replace(/<li[^>]*>/gi, '• ')
      .replace(/<[^>]+>/g, '')
  )
    .replace(/[ \t]+/g, ' ')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

export function parseDescription(html: string): DescBlock[] {
  if (!html) return [];
  // Split on headings or paragraph-leading <strong> titles.
  const parts = html.split(/(?=<h[1-6][^>]*>)|(?=<p[^>]*>\s*<strong[^>]*>)/gi);
  const blocks: DescBlock[] = [];
  for (const part of parts) {
    if (!part.trim()) continue;
    const hm = part.match(/^<h[1-6][^>]*>(.*?)<\/h[1-6]>/is) || part.match(/^<p[^>]*>\s*<strong[^>]*>(.*?)<\/strong>/is);
    let heading: string | null = null;
    let rest = part;
    if (hm) {
      heading = stripTags(hm[1]).replace(/[:：]\s*$/, '');
      rest = part.slice(hm[0].length);
      // Undo the split if the "heading" is actually a full sentence.
      if (heading.length > 60) {
        heading = null;
        rest = part;
      }
    }
    const text = stripTags(rest);
    if (!heading && !text) continue;
    const prev = blocks[blocks.length - 1];
    if (!heading && prev && prev.heading === null) {
      prev.text = `${prev.text}\n\n${text}`.trim();
    } else {
      blocks.push({ heading, text });
    }
  }
  return blocks;
}
