// Collection listing — merchandised order from Shopify, live refresh,
// sort + filter chips, 2-col grid with entrance animations.
import React, { useEffect, useMemo, useState } from 'react';
import { Dimensions, FlatList, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { useLocalSearchParams, useRouter } from 'expo-router';
import { SafeAreaView } from 'react-native-safe-area-context';
import { Ionicons } from '@expo/vector-icons';
import { collectionProducts, fetchLiveCollection, getCollection } from '../../src/api/shopify';
import type { Product } from '../../src/api/types';
import { ProductCard } from '../../src/components/ProductCard';
import { Skeleton } from '../../src/components/ui';
import { colors, radius, type as t, SCREEN_W } from '../../src/theme';

const W = SCREEN_W;
const CARD_W = (W - 52) / 2;

type Sort = 'featured' | 'price-asc' | 'price-desc' | 'discount' | 'new';
const SORTS: { key: Sort; label: string }[] = [
  { key: 'featured', label: 'Featured' },
  { key: 'price-asc', label: 'Price ↑' },
  { key: 'price-desc', label: 'Price ↓' },
  { key: 'discount', label: 'Discount' },
  { key: 'new', label: 'Newest' },
];

export default function CollectionScreen() {
  const { handle } = useLocalSearchParams<{ handle: string }>();
  const router = useRouter();
  const collection = getCollection(handle!);
  const [products, setProducts] = useState<Product[]>(() => collectionProducts(handle!));
  const [loading, setLoading] = useState(products.length === 0);
  const [sort, setSort] = useState<Sort>('featured');
  const [inStockOnly, setInStockOnly] = useState(false);
  const [maxPrice, setMaxPrice] = useState<number | null>(null);

  useEffect(() => {
    let alive = true;
    fetchLiveCollection(handle!).then((live) => {
      if (alive && live) setProducts(live);
      if (alive) setLoading(false);
    });
    return () => {
      alive = false;
    };
  }, [handle]);

  const priceSteps = useMemo(() => {
    const prices = products.map((p) => p.price).sort((a, b) => a - b);
    if (!prices.length) return [];
    const hi = prices[prices.length - 1];
    return [499, 999, 1999].filter((x) => x < hi);
  }, [products]);

  const shown = useMemo(() => {
    let list = [...products];
    if (inStockOnly) list = list.filter((p) => p.variants.some((v) => v.available));
    if (maxPrice) list = list.filter((p) => p.price <= maxPrice);
    switch (sort) {
      case 'price-asc':
        list.sort((a, b) => a.price - b.price);
        break;
      case 'price-desc':
        list.sort((a, b) => b.price - a.price);
        break;
      case 'discount':
        list.sort(
          (a, b) =>
            (b.compareAt ? (b.compareAt - b.price) / b.compareAt : 0) -
            (a.compareAt ? (a.compareAt - a.price) / a.compareAt : 0)
        );
        break;
      case 'new':
        list.sort((a, b) => (b.createdAt > a.createdAt ? 1 : -1));
        break;
    }
    return list;
  }, [products, sort, inStockOnly, maxPrice]);

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: colors.bg }} edges={['top']}>
      <View style={s.header}>
        <Pressable hitSlop={12} onPress={() => router.back()}>
          <Ionicons name="chevron-back" size={24} color={colors.text} />
        </Pressable>
        <View style={{ flex: 1 }}>
          <Text style={s.title} numberOfLines={1}>
            {collection?.title ?? handle}
          </Text>
          <Text style={s.count}>{shown.length} products</Text>
        </View>
        <Pressable hitSlop={12} onPress={() => router.push('/cart')}>
          <Ionicons name="bag-handle-outline" size={22} color={colors.text} />
        </Pressable>
      </View>

      <View style={{ height: 44 }}>
        <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={s.chips}>
          {SORTS.map((x) => (
            <Pressable key={x.key} style={[s.chip, sort === x.key && s.chipOn]} onPress={() => setSort(x.key)}>
              <Text style={[s.chipText, sort === x.key && s.chipTextOn]}>{x.label}</Text>
            </Pressable>
          ))}
          <Pressable style={[s.chip, inStockOnly && s.chipOn]} onPress={() => setInStockOnly((v) => !v)}>
            <Text style={[s.chipText, inStockOnly && s.chipTextOn]}>In stock</Text>
          </Pressable>
          {priceSteps.map((p) => (
            <Pressable
              key={p}
              style={[s.chip, maxPrice === p && s.chipOn]}
              onPress={() => setMaxPrice(maxPrice === p ? null : p)}
            >
              <Text style={[s.chipText, maxPrice === p && s.chipTextOn]}>Under ₹{p}</Text>
            </Pressable>
          ))}
        </ScrollView>
      </View>

      {loading ? (
        <View style={s.skeletonGrid}>
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} style={{ width: CARD_W, height: CARD_W * 1.45 }} />
          ))}
        </View>
      ) : (
        <FlatList
          data={shown}
          numColumns={2}
          keyExtractor={(p) => p.handle}
          columnWrapperStyle={{ gap: 12, paddingHorizontal: 20 }}
          contentContainerStyle={{ gap: 12, paddingTop: 6, paddingBottom: 130 }}
          renderItem={({ item, index }) => <ProductCard product={item} width={CARD_W} index={index % 6} />}
          ListEmptyComponent={<Text style={s.empty}>No products match these filters.</Text>}
        />
      )}
    </SafeAreaView>
  );
}

const s = StyleSheet.create({
  header: { flexDirection: 'row', alignItems: 'center', gap: 14, paddingHorizontal: 20, paddingVertical: 10 },
  title: { ...t.display, color: colors.text, fontSize: 19 },
  count: { color: colors.textFaint, fontSize: 11.5, marginTop: 2 },
  chips: { paddingHorizontal: 20, gap: 8, alignItems: 'center' },
  chip: {
    paddingHorizontal: 14,
    paddingVertical: 8,
    borderRadius: radius.pill,
    borderWidth: 1,
    borderColor: colors.line,
    backgroundColor: colors.surface,
  },
  chipOn: { borderColor: colors.gold, backgroundColor: colors.chip },
  chipText: { color: colors.textDim, fontSize: 12.5, fontWeight: '600' },
  chipTextOn: { color: colors.gold },
  skeletonGrid: { flexDirection: 'row', flexWrap: 'wrap', gap: 12, padding: 20 },
  empty: { color: colors.textDim, textAlign: 'center', marginTop: 60 },
});
