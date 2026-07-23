// Config-driven homepage sections mirroring studdmuffyn.com's live theme.
// Each renderer maps 1:1 to a JSON section type — new merchandising
// layouts ship as config, not code.
import React, { useEffect, useRef, useState } from 'react';
import {
  FlatList,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from 'react-native';
import { Image } from 'expo-image';
import { LinearGradient } from 'expo-linear-gradient';
import { useRouter } from 'expo-router';
import Animated, { FadeIn } from 'react-native-reanimated';
import { collectionProducts, fetchLiveCollection, getCollection } from '../api/shopify';
import type { Product } from '../api/types';
import { colors, darkGradient, radius, shadow, type as t, SCREEN_W } from '../theme';
import { ProductCard } from './ProductCard';
import { SectionHeader } from './ui';

const W = SCREEN_W;

export function goTo(router: ReturnType<typeof useRouter>, url: string) {
  const m = url.match(/\/collections\/([a-z0-9-]+)/);
  if (m) return router.push(`/collection/${m[1]}`);
  const p = url.match(/\/products\/([a-z0-9-]+)/);
  if (p) return router.push(`/product/${p[1]}`);
}

// --- announcement ticker (site: gold bar, cream text) -----------------------
export function AnnouncementTicker({ messages }: { messages: string[] }) {
  const [i, setI] = useState(0);
  useEffect(() => {
    if (messages.length < 2) return;
    const id = setInterval(() => setI((x) => (x + 1) % messages.length), 3500);
    return () => clearInterval(id);
  }, [messages.length]);
  return (
    <View style={s.ticker}>
      <Animated.Text key={i} entering={FadeIn.duration(400)} style={s.tickerText} numberOfLines={1}>
        {messages[i]}
      </Animated.Text>
    </View>
  );
}

// --- hero / slideshow --------------------------------------------------------
export function Hero({ slides, aspect = 1.705 }: { slides: { image: string; url: string }[]; aspect?: number }) {
  const router = useRouter();
  const ref = useRef<FlatList>(null);
  const [page, setPage] = useState(0);
  useEffect(() => {
    if (slides.length < 2) return;
    const id = setInterval(() => {
      const next = (page + 1) % slides.length;
      ref.current?.scrollToIndex({ index: next, animated: true });
      setPage(next);
    }, 4500);
    return () => clearInterval(id);
  }, [page, slides.length]);
  return (
    <View>
      <FlatList
        ref={ref}
        data={slides}
        keyExtractor={(x) => x.image}
        horizontal
        pagingEnabled
        showsHorizontalScrollIndicator={false}
        onMomentumScrollEnd={(e) => setPage(Math.round(e.nativeEvent.contentOffset.x / W))}
        renderItem={({ item }) => (
          <Pressable onPress={() => goTo(router, item.url)}>
            <Image source={{ uri: item.image }} style={{ width: W, aspectRatio: aspect, backgroundColor: colors.surface }} contentFit="cover" transition={300} />
          </Pressable>
        )}
      />
      {slides.length > 1 && (
        <View style={s.dots}>
          {slides.map((_, i) => (
            <View key={i} style={[s.dot, i === page && s.dotActive]} />
          ))}
        </View>
      )}
    </View>
  );
}

// --- top offer-tile row (site's scrollable shop-by-offer tiles) --------------
export function IconRow({ items }: { items: { image: string; title?: string; handle: string }[] }) {
  const router = useRouter();
  return (
    <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={s.iconRow}>
      {items.map((it) => (
        <Pressable key={it.handle} style={s.iconItem} onPress={() => router.push(`/collection/${it.handle}`)}>
          <View style={s.iconTile}>
            <Image source={{ uri: it.image }} style={{ flex: 1 }} contentFit="cover" transition={200} />
          </View>
          {it.title ? (
            <Text style={s.iconLabel} numberOfLines={2}>
              {it.title}
            </Text>
          ) : null}
        </Pressable>
      ))}
    </ScrollView>
  );
}

// --- auto-scrolling strip (USP badges / marketplace logos) ------------------
export function LogoStrip({ images, height = 84, gap = 18 }: { images: string[]; height?: number; gap?: number }) {
  const ref = useRef<ScrollView>(null);
  const x = useRef(0);
  useEffect(() => {
    const id = setInterval(() => {
      x.current += 1;
      ref.current?.scrollTo({ x: x.current, animated: false });
    }, 30);
    return () => clearInterval(id);
  }, []);
  const doubled = [...images, ...images];
  return (
    <ScrollView
      ref={ref}
      horizontal
      showsHorizontalScrollIndicator={false}
      scrollEnabled={false}
      onContentSizeChange={(w) => {
        // loop the marquee
        if (x.current > w / 2) x.current = 0;
      }}
      contentContainerStyle={{ paddingHorizontal: 16, gap, alignItems: 'center', paddingVertical: 10 }}
    >
      {doubled.map((img, i) => (
        <Image
          key={i}
          source={{ uri: img }}
          style={{ height, aspectRatio: 1.4, borderRadius: radius.sm }}
          contentFit="contain"
          transition={200}
        />
      ))}
    </ScrollView>
  );
}

// --- full-width tappable image banner (site image_hero sections) ------------
export function ImageBanner({ image, url, aspect = 1 }: { image: string; url?: string; aspect?: number }) {
  const router = useRouter();
  return (
    <Pressable disabled={!url} onPress={() => url && goTo(router, url)} style={{ marginTop: 26 }}>
      <Image
        source={{ uri: image }}
        style={{ width: W, aspectRatio: aspect, backgroundColor: colors.surface }}
        contentFit="cover"
        transition={300}
      />
    </Pressable>
  );
}

// --- product rail -----------------------------------------------------------
export function ProductRail({
  title,
  subtitle,
  handle,
  products,
}: {
  title: string;
  subtitle?: string;
  handle?: string;
  products?: Product[];
}) {
  const router = useRouter();
  const bundled = products ?? (handle ? collectionProducts(handle) : []);
  const [fetched, setFetched] = useState<Product[] | null>(null);
  useEffect(() => {
    // collection not in the bundled snapshot (e.g. created on the site after
    // this app build) → pull it live from Shopify
    if (!products && handle && bundled.length === 0) {
      let alive = true;
      fetchLiveCollection(handle).then((live) => {
        if (alive && live?.length) setFetched(live);
      });
      return () => {
        alive = false;
      };
    }
  }, [handle]);
  const items = (bundled.length ? bundled : fetched ?? []).slice(0, 12);
  if (!items.length) return null;
  return (
    <View>
      <SectionHeader
        title={title}
        subtitle={subtitle}
        actionLabel={handle ? 'View all' : undefined}
        onAction={handle ? () => router.push(`/collection/${handle}`) : undefined}
      />
      <FlatList
        data={items}
        horizontal
        showsHorizontalScrollIndicator={false}
        keyExtractor={(p) => p.handle}
        contentContainerStyle={{ paddingHorizontal: 20, gap: 14 }}
        renderItem={({ item, index }) => <ProductCard product={item} index={index} />}
      />
    </View>
  );
}

// --- category / edit grid -----------------------------------------------------
function tileImage(handle: string): string | undefined {
  const c = getCollection(handle);
  if (c?.image) return c.image;
  return collectionProducts(handle)[0]?.images[0];
}

export function CategoryGrid({
  title,
  items,
  aspect = 1.5,
  showLabel = true,
}: {
  title?: string;
  items: { title: string; handle: string; image?: string }[];
  aspect?: number;
  showLabel?: boolean;
}) {
  const router = useRouter();
  return (
    <View>
      {title ? <SectionHeader title={title} /> : null}
      <View style={s.grid}>
        {items.map((it) => (
          <Pressable key={it.handle} style={[s.catTile, { aspectRatio: aspect }]} onPress={() => router.push(`/collection/${it.handle}`)}>
            <Image source={{ uri: it.image ?? tileImage(it.handle) }} style={s.catImage} contentFit="cover" transition={250} />
            {showLabel && (
              <>
                <LinearGradient colors={darkGradient} style={StyleSheet.absoluteFill} />
                <Text style={s.catLabel}>{it.title}</Text>
              </>
            )}
          </Pressable>
        ))}
      </View>
    </View>
  );
}

// --- purpose chips ------------------------------------------------------------
export function PurposeGrid({
  title,
  subtitle,
  items,
}: {
  title: string;
  subtitle?: string;
  items: { title: string; handle: string }[];
}) {
  const router = useRouter();
  return (
    <View>
      <SectionHeader title={title} subtitle={subtitle} />
      <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={{ paddingHorizontal: 20, gap: 10 }}>
        {items.map((it) => (
          <Pressable key={it.handle} style={s.purposeChip} onPress={() => router.push(`/collection/${it.handle}`)}>
            <Text style={s.purposeText}>{it.title}</Text>
          </Pressable>
        ))}
      </ScrollView>
    </View>
  );
}

// --- offer cards ---------------------------------------------------------------
export function OfferCards({
  title,
  items,
}: {
  title: string;
  items: { title: string; sub: string; handle: string }[];
}) {
  const router = useRouter();
  return (
    <View>
      <SectionHeader title={title} />
      <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={{ paddingHorizontal: 20, gap: 12 }}>
        {items.map((it) => (
          <Pressable key={it.handle} onPress={() => router.push(`/collection/${it.handle}`)}>
            <View style={[s.offerCard, shadow.card]}>
              <Text style={s.offerTitle}>{it.title}</Text>
              <Text style={s.offerSub}>{it.sub}</Text>
              <Text style={s.offerCta}>Shop now ›</Text>
            </View>
          </Pressable>
        ))}
      </ScrollView>
    </View>
  );
}

// --- full-bleed banner with overlay text ----------------------------------------
export function BigBanner({ title, subtitle, handle }: { title: string; subtitle?: string; handle: string }) {
  const router = useRouter();
  const img = tileImage(handle);
  if (!img) return null;
  return (
    <Pressable style={s.bigBanner} onPress={() => router.push(`/collection/${handle}`)}>
      <Image source={{ uri: img }} style={StyleSheet.absoluteFill} contentFit="cover" transition={300} />
      <LinearGradient colors={darkGradient} style={StyleSheet.absoluteFill} />
      <View style={s.bigBannerText}>
        <Text style={s.bigBannerTitle}>{title}</Text>
        {subtitle ? <Text style={s.bigBannerSub}>{subtitle}</Text> : null}
        <Text style={[s.offerCta, { color: '#fff' }]}>Explore ›</Text>
      </View>
    </Pressable>
  );
}

const s = StyleSheet.create({
  ticker: { backgroundColor: colors.gold, paddingVertical: 8, alignItems: 'center', paddingHorizontal: 10 },
  tickerText: { color: colors.cream, fontSize: 11.5, fontWeight: '700', letterSpacing: 0.6 },
  dots: { position: 'absolute', bottom: 12, alignSelf: 'center', flexDirection: 'row', gap: 6 },
  dot: { width: 6, height: 6, borderRadius: 3, backgroundColor: 'rgba(255,255,255,0.55)' },
  dotActive: { backgroundColor: colors.gold, width: 18 },
  iconRow: { paddingHorizontal: 14, gap: 10, paddingVertical: 12 },
  iconItem: { width: 148, alignItems: 'center' },
  iconTile: {
    width: 148,
    aspectRatio: 1.84,
    borderRadius: radius.sm,
    overflow: 'hidden',
    borderWidth: 1,
    borderColor: colors.line,
    backgroundColor: colors.surface,
  },
  iconLabel: { color: colors.text, fontSize: 11, textAlign: 'center', marginTop: 7, lineHeight: 14, fontWeight: '500' },
  grid: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    paddingHorizontal: 20,
    gap: 12,
    marginTop: 4,
  },
  catTile: {
    width: Math.floor((W - 54) / 2),
    borderRadius: radius.md,
    overflow: 'hidden',
    backgroundColor: colors.surface,
    justifyContent: 'flex-end',
  },
  catImage: { position: 'absolute', top: 0, left: 0, right: 0, bottom: 0 },
  catLabel: { ...t.display, color: '#fff', fontSize: 16, padding: 12 },
  purposeChip: {
    borderWidth: 1,
    borderColor: colors.gold,
    backgroundColor: colors.chip,
    borderRadius: radius.pill,
    paddingHorizontal: 18,
    paddingVertical: 12,
  },
  purposeText: { color: colors.goldSoft, fontSize: 13, fontWeight: '600' },
  offerCard: {
    width: 250,
    borderRadius: radius.md,
    padding: 18,
    backgroundColor: colors.header,
    borderWidth: 1,
    borderColor: colors.line,
  },
  offerTitle: { ...t.display, color: colors.text, fontSize: 19 },
  offerSub: { color: colors.textDim, fontSize: 12.5, marginTop: 4 },
  offerCta: { color: colors.goldSoft, fontWeight: '700', fontSize: 13, marginTop: 14 },
  bigBanner: {
    marginHorizontal: 20,
    marginTop: 30,
    height: 200,
    borderRadius: radius.lg,
    overflow: 'hidden',
    justifyContent: 'flex-end',
  },
  bigBannerText: { padding: 18 },
  bigBannerTitle: { ...t.display, color: '#fff', fontSize: 23 },
  bigBannerSub: { color: 'rgba(255,255,255,0.8)', marginTop: 4, fontSize: 13 },
});
