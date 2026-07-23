// Product page — swipe gallery, live price/stock, variant selector,
// accordion description, trust badges, recommendations, sticky ATC/Buy Now.
import React, { useEffect, useMemo, useState } from 'react';
import {
  Dimensions,
  FlatList,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from 'react-native';
import { Image } from 'expo-image';
import { useLocalSearchParams, useRouter } from 'expo-router';
import { SafeAreaView, useSafeAreaInsets } from 'react-native-safe-area-context';
import { Ionicons } from '@expo/vector-icons';
import * as Haptics from 'expo-haptics';
import * as WebBrowser from 'expo-web-browser';
import Animated, { FadeIn, FadeInDown } from 'react-native-reanimated';
import {
  allProducts,
  fetchLiveProduct,
  getProduct,
  startCheckoutUrl,
} from '../../src/api/shopify';
import { parseDescription } from '../../src/api/html';
import { useProductExtras } from '../../src/api/extras';
import type { Product, Variant } from '../../src/api/types';
import { ProductRail } from '../../src/components/HomeSections';
import { Badge, GoldButton } from '../../src/components/ui';
import { useShop } from '../../src/store/shop';
import { colors, INR, pctOff, radius, type as t, SCREEN_W } from '../../src/theme';

const W = SCREEN_W;

const TRUST = [
  { icon: 'shield-checkmark-outline', label: 'Secure Shopify checkout' },
  { icon: 'cash-outline', label: 'COD available' },
  { icon: 'rocket-outline', label: 'Fast nationwide shipping' },
  { icon: 'sparkles-outline', label: 'Authentic products' },
] as const;

function Accordion({ heading, text, initiallyOpen }: { heading: string; text: string; initiallyOpen?: boolean }) {
  const [open, setOpen] = useState(!!initiallyOpen);
  return (
    <View style={s.accordion}>
      <Pressable style={s.accordionHead} onPress={() => setOpen((v) => !v)}>
        <Text style={s.accordionTitle}>{heading}</Text>
        <Ionicons name={open ? 'remove' : 'add'} size={20} color={colors.gold} />
      </Pressable>
      {open && (
        <Animated.Text entering={FadeIn.duration(200)} style={s.accordionBody}>
          {text}
        </Animated.Text>
      )}
    </View>
  );
}

export default function ProductScreen() {
  const { handle } = useLocalSearchParams<{ handle: string }>();
  const router = useRouter();
  const insets = useSafeAreaInsets();
  const [product, setProduct] = useState<Product | undefined>(() => getProduct(handle!));
  const [gi, setGi] = useState(0);
  const [added, setAdded] = useState(false);
  const { addToCart, toggleWish, wishlist, pushRecent } = useShop();

  useEffect(() => {
    pushRecent(handle!);
    fetchLiveProduct(handle!).then((p) => p && setProduct({ ...p }));
  }, [handle]);

  const [variantId, setVariantId] = useState<number | undefined>();
  const variant: Variant | undefined = useMemo(() => {
    if (!product) return undefined;
    return (
      product.variants.find((v) => v.id === variantId) ??
      product.variants.find((v) => v.available) ??
      product.variants[0]
    );
  }, [product, variantId]);

  const extras = useProductExtras(handle);

  const blocks = useMemo(() => parseDescription(product?.descriptionHtml ?? ''), [product]);
  const intro = blocks.find((b) => !b.heading);
  const sections = useMemo(() => {
    const base = blocks.filter((b) => b.heading && b.text);
    for (const s of extras?.sections ?? []) {
      if (!base.find((b) => b.heading!.toLowerCase().includes(s.heading.toLowerCase().slice(0, 12))))
        base.push({ heading: s.heading, text: s.text });
    }
    return base;
  }, [blocks, extras]);

  const pairsProducts = useMemo(
    () => (extras?.pairsWith ?? []).map((h) => getProduct(h)).filter(Boolean) as Product[],
    [extras]
  );

  const recommendations = useMemo(() => {
    if (!product) return [];
    return allProducts()
      .filter((p) => p.handle !== product.handle && (p.productType === product.productType || p.vendor === product.vendor))
      .slice(0, 10);
  }, [product]);

  if (!product) {
    return (
      <SafeAreaView style={{ flex: 1, backgroundColor: colors.bg, alignItems: 'center', justifyContent: 'center' }}>
        <Text style={{ color: colors.textDim }}>Product not found.</Text>
      </SafeAreaView>
    );
  }

  const wished = wishlist.includes(product.handle);
  const off = pctOff(variant?.price ?? product.price, variant?.compareAt ?? product.compareAt);
  const soldOut = !variant?.available;

  const add = () => {
    if (!variant) return;
    Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success);
    addToCart({
      handle: product.handle,
      variantId: variant.id,
      variantTitle: variant.title,
      title: product.title,
      image: product.images[0],
      price: variant.price,
      compareAt: variant.compareAt,
    });
    setAdded(true);
    setTimeout(() => setAdded(false), 1400);
  };

  const buyNow = async () => {
    if (!variant) return;
    Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Heavy);
    await WebBrowser.openBrowserAsync(await startCheckoutUrl([{ variantId: variant.id, qty: 1 }]));
  };

  return (
    <View style={{ flex: 1, backgroundColor: colors.bg }}>
      <ScrollView showsVerticalScrollIndicator={false} contentContainerStyle={{ paddingBottom: 170 }}>
        {/* gallery */}
        <View>
          <FlatList
            data={product.images.length ? product.images : [undefined]}
            horizontal
            pagingEnabled
            showsHorizontalScrollIndicator={false}
            keyExtractor={(x, i) => String(x ?? i)}
            onMomentumScrollEnd={(e) => setGi(Math.round(e.nativeEvent.contentOffset.x / W))}
            renderItem={({ item }) => (
              <Image source={{ uri: item }} style={s.galleryImg} contentFit="cover" transition={250} />
            )}
          />
          <View style={[s.topBar, { top: insets.top + 6 }]}>
            <Pressable style={s.roundBtn} onPress={() => router.back()}>
              <Ionicons name="chevron-back" size={22} color="#fff" />
            </Pressable>
            <View style={{ flex: 1 }} />
            <Pressable
              style={s.roundBtn}
              onPress={() => {
                Haptics.selectionAsync();
                toggleWish(product.handle);
              }}
            >
              <Ionicons name={wished ? 'heart' : 'heart-outline'} size={20} color={wished ? colors.gold : '#fff'} />
            </Pressable>
            <Pressable style={[s.roundBtn, { marginLeft: 10 }]} onPress={() => router.push('/cart')}>
              <Ionicons name="bag-handle-outline" size={20} color="#fff" />
            </Pressable>
          </View>
          {product.images.length > 1 && (
            <View style={s.dots}>
              {product.images.map((_, i) => (
                <View key={i} style={[s.dot, i === gi && s.dotOn]} />
              ))}
            </View>
          )}
        </View>

        {/* core info */}
        <Animated.View entering={FadeInDown.duration(350)} style={{ paddingHorizontal: 20, paddingTop: 18 }}>
          <Text style={s.vendor}>{product.vendor.toUpperCase()}</Text>
          <Text style={s.title}>{product.title}</Text>
          <View style={s.priceRow}>
            <Text style={[s.price, (variant?.compareAt ?? 0) > (variant?.price ?? 0) ? { color: colors.sale } : null]}>{INR(variant?.price ?? product.price)}</Text>
            {variant?.compareAt && variant.compareAt > variant.price ? (
              <Text style={s.compare}>{INR(variant.compareAt)}</Text>
            ) : null}
            {off > 0 && <Badge label={`${off}% OFF`} tone="gold" />}
            {soldOut && <Badge label="SOLD OUT" tone="danger" />}
          </View>
          <Text style={s.taxNote}>Inclusive of all taxes</Text>
          {extras && extras.reviewCount > 0 ? (
            <View style={s.ratingRow}>
              <Text style={s.stars}>{'★'.repeat(Math.round(extras.rating))}{'☆'.repeat(5 - Math.round(extras.rating))}</Text>
              <Text style={s.ratingText}>{extras.rating.toFixed(1)} · {extras.reviewCount} reviews</Text>
            </View>
          ) : null}
        </Animated.View>

        {/* variant selector */}
        {product.variants.length > 1 && (
          <View style={{ paddingHorizontal: 20, marginTop: 16 }}>
            <Text style={s.optionName}>{product.options[0]?.name ?? 'Options'}</Text>
            <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={{ gap: 8, paddingVertical: 10 }}>
              {product.variants.map((v) => {
                const on = v.id === variant?.id;
                return (
                  <Pressable
                    key={v.id}
                    style={[s.variantChip, on && s.variantOn, !v.available && s.variantOff]}
                    onPress={() => v.available && setVariantId(v.id)}
                  >
                    <Text style={[s.variantText, on && { color: colors.gold }, !v.available && { textDecorationLine: 'line-through' }]}>
                      {v.title}
                    </Text>
                  </Pressable>
                );
              })}
            </ScrollView>
          </View>
        )}

        {/* trust badges */}
        <View style={s.trustRow}>
          {TRUST.map((tr) => (
            <View key={tr.label} style={s.trustItem}>
              <Ionicons name={tr.icon} size={18} color={colors.gold} />
              <Text style={s.trustText}>{tr.label}</Text>
            </View>
          ))}
        </View>

        {/* description */}
        {intro?.text ? (
          <Text style={s.intro}>{intro.text}</Text>
        ) : null}
        <View style={{ paddingHorizontal: 20, marginTop: 8 }}>
          {sections.map((b, i) => (
            <Accordion key={i} heading={b.heading!} text={b.text} initiallyOpen={i === 0} />
          ))}
        </View>

        {pairsProducts.length > 0 && (
          <ProductRail title="Pairs well with" products={pairsProducts} />
        )}

        {extras && extras.reviews.length > 0 && (
          <View style={{ paddingHorizontal: 20, marginTop: 30 }}>
            <Text style={s.revHead}>Customer Reviews</Text>
            <Text style={s.revSummary}>
              {'★'.repeat(Math.round(extras.rating))} {extras.rating.toFixed(1)} out of 5 · based on {extras.reviewCount} reviews
            </Text>
            {extras.reviews.slice(0, 6).map((r, i) => (
              <View key={i} style={s.revCard}>
                <View style={s.revTop}>
                  <Text style={s.revStars}>{'★'.repeat(r.score)}</Text>
                  <Text style={s.revAuthor}>{r.author || 'Verified buyer'}</Text>
                  <Text style={s.revDate}>{r.date}</Text>
                </View>
                {r.title ? <Text style={s.revTitle}>{r.title}</Text> : null}
                <Text style={s.revBody}>{r.body}</Text>
              </View>
            ))}
          </View>
        )}

        {recommendations.length > 0 && (
          <ProductRail title="You may also like" products={recommendations} />
        )}
      </ScrollView>

      {/* sticky buy bar */}
      <View style={[s.buyBar, { paddingBottom: insets.bottom + 12 }]}>
        <Pressable style={[s.atcBtn, added && { borderColor: colors.success }]} onPress={add} disabled={soldOut}>
          <Text style={[s.atcText, added && { color: colors.success }]}>
            {soldOut ? 'Sold out' : added ? 'Added ✓' : 'Add to bag'}
          </Text>
        </Pressable>
        <GoldButton label="BUY NOW" onPress={buyNow} style={{ flex: 1 }} />
      </View>
    </View>
  );
}

const s = StyleSheet.create({
  galleryImg: { width: W, aspectRatio: 0.95, backgroundColor: colors.surfaceHi },
  topBar: { position: 'absolute', left: 16, right: 16, flexDirection: 'row' },
  roundBtn: {
    width: 38,
    height: 38,
    borderRadius: 19,
    backgroundColor: 'rgba(0,0,0,0.45)',
    alignItems: 'center',
    justifyContent: 'center',
  },
  dots: { position: 'absolute', bottom: 14, alignSelf: 'center', flexDirection: 'row', gap: 6 },
  dot: { width: 6, height: 6, borderRadius: 3, backgroundColor: 'rgba(255,255,255,0.4)' },
  dotOn: { backgroundColor: colors.gold, width: 18 },
  vendor: { color: colors.goldSoft, fontSize: 11, fontWeight: '800', letterSpacing: 1.6 },
  title: { ...t.display, color: colors.text, fontSize: 24, lineHeight: 31, marginTop: 6 },
  priceRow: { flexDirection: 'row', alignItems: 'center', gap: 10, marginTop: 12 },
  price: { ...t.display, color: colors.text, fontSize: 26, fontWeight: '700' },
  compare: { color: colors.textFaint, fontSize: 15, textDecorationLine: 'line-through' },
  taxNote: { color: colors.textFaint, fontSize: 11.5, marginTop: 4 },
  ratingRow: { flexDirection: 'row', alignItems: 'center', gap: 8, marginTop: 10 },
  stars: { color: '#ffcb42', fontSize: 15, letterSpacing: 1 },
  ratingText: { color: colors.textDim, fontSize: 12.5, fontWeight: '600' },
  revHead: { ...t.display, color: colors.text, fontSize: 22 },
  revSummary: { color: colors.textDim, fontSize: 13, marginTop: 6, marginBottom: 14 },
  revCard: {
    backgroundColor: colors.surface,
    borderRadius: radius.md,
    padding: 14,
    marginBottom: 10,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: colors.line,
  },
  revTop: { flexDirection: 'row', alignItems: 'center', gap: 8 },
  revStars: { color: '#ffcb42', fontSize: 13, letterSpacing: 1 },
  revAuthor: { color: colors.text, fontSize: 12.5, fontWeight: '700', flex: 1 },
  revDate: { color: colors.textFaint, fontSize: 11 },
  revTitle: { color: colors.text, fontSize: 13.5, fontWeight: '700', marginTop: 8 },
  revBody: { color: colors.textDim, fontSize: 13, lineHeight: 20, marginTop: 6 },
  optionName: { color: colors.textDim, fontSize: 12, fontWeight: '700', letterSpacing: 0.6 },
  variantChip: {
    paddingHorizontal: 16,
    paddingVertical: 10,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colors.line,
    backgroundColor: colors.surface,
  },
  variantOn: { borderColor: colors.gold, backgroundColor: colors.chip },
  variantOff: { opacity: 0.45 },
  variantText: { color: colors.text, fontSize: 13, fontWeight: '600' },
  trustRow: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    marginHorizontal: 20,
    marginTop: 20,
    backgroundColor: colors.surface,
    borderRadius: radius.lg,
    padding: 14,
    gap: 10,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: colors.line,
  },
  trustItem: { flexDirection: 'row', alignItems: 'center', gap: 8, width: '47%' },
  trustText: { color: colors.textDim, fontSize: 11.5, flex: 1 },
  intro: { color: colors.textDim, fontSize: 14, lineHeight: 22, paddingHorizontal: 20, marginTop: 20 },
  accordion: { borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: colors.line },
  accordionHead: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', paddingVertical: 16 },
  accordionTitle: { ...t.display, color: colors.text, fontSize: 16, flex: 1, paddingRight: 12 },
  accordionBody: { color: colors.textDim, fontSize: 13.5, lineHeight: 21, paddingBottom: 16 },
  buyBar: {
    position: 'absolute',
    left: 0,
    right: 0,
    bottom: 0,
    flexDirection: 'row',
    gap: 12,
    paddingHorizontal: 20,
    paddingTop: 12,
    backgroundColor: 'rgba(255,255,255,0.96)',
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: colors.line,
  },
  atcBtn: {
    flex: 1,
    borderRadius: 999,
    borderWidth: 1.5,
    borderColor: colors.gold,
    alignItems: 'center',
    justifyContent: 'center',
  },
  atcText: { color: colors.gold, fontWeight: '800', fontSize: 14, letterSpacing: 0.6 },
});
