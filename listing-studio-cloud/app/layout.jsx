import "./globals.css";

export const metadata = { title: "Listing Studio" };

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>
        <nav>
          <span className="brand">⛓ Listing Studio</span>
          <a href="/">Dashboard</a>
          <a href="/listings">All listings</a>
          <a href="/guide">Rules &amp; guide</a>
          <a href="/new" className="cta">+ New product</a>
        </nav>
        <div className="wrap">{children}</div>
      </body>
    </html>
  );
}
