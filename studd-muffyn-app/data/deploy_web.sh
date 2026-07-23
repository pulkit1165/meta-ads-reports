#!/bin/bash
# One-command web deploy for the Studd Muffyn app (demo + config/extras host).
# Usage: bash data/deploy_web.sh
set -e
cd "$(dirname "$0")/../app"

rm -rf dist
npx expo export --platform web --output-dir dist

# Vercel refuses to serve node_modules paths — relocate the icon fonts
mkdir -p dist/assets/vendorfonts dist/api
cp -r dist/assets/node_modules/* dist/assets/vendorfonts/
rm -rf dist/assets/node_modules
LC_ALL=C sed -i '' 's|assets/node_modules|assets/vendorfonts|g' dist/_expo/static/js/web/*.js

# serverless functions + hosted checkout pages
cp api/*.js dist/api/
cp web/*.html dist/

# product extras (reviews / pairs-well-with / detail sections)
if [ -d ../data/extras ]; then
  mkdir -p dist/extras
  cp ../data/extras/*.json dist/extras/ 2>/dev/null || true
  [ -f ../data/extras-index.json ] && cp ../data/extras-index.json dist/extras/index.json
  echo "extras: $(ls dist/extras | wc -l | tr -d ' ') files"
fi

# SPA rewrite (static files & /api take precedence automatically)
node -e "require('fs').writeFileSync('dist/vercel.json', JSON.stringify({rewrites:[{source:'/((?!api/).*)',destination:'/index.html'}]},null,2))"

cp -r .vercel-link-backup dist/.vercel
cd dist && vercel deploy --prod --yes
