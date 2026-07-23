import React, { useMemo } from 'react';
import { Dimensions, FlatList, StyleSheet, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { Ionicons } from '@expo/vector-icons';
import { getProduct } from '../../src/api/shopify';
import type { Product } from '../../src/api/types';
import { ProductCard } from '../../src/components/ProductCard';
import { useShop } from '../../src/store/shop';
import { colors, type as t, SCREEN_W } from '../../src/theme';

const W = SCREEN_W;
const CARD_W = (W - 52) / 2;

export default function Wishlist() {
  const wishlist = useShop((s) => s.wishlist);
  const products = useMemo(
    () => wishlist.map((h) => getProduct(h)).filter(Boolean) as Product[],
    [wishlist]
  );

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: colors.bg }} edges={['top']}>
      <Text style={s.title}>Wishlist</Text>
      {products.length === 0 ? (
        <View style={s.emptyWrap}>
          <Ionicons name="heart-outline" size={44} color={colors.textFaint} />
          <Text style={s.emptyTitle}>Nothing saved yet</Text>
          <Text style={s.emptySub}>Tap the heart on any product to keep it here.</Text>
        </View>
      ) : (
        <FlatList
          data={products}
          numColumns={2}
          keyExtractor={(p) => p.handle}
          columnWrapperStyle={{ gap: 12, paddingHorizontal: 20 }}
          contentContainerStyle={{ gap: 12, paddingTop: 6, paddingBottom: 130 }}
          renderItem={({ item, index }) => <ProductCard product={item} width={CARD_W} index={index % 6} />}
        />
      )}
    </SafeAreaView>
  );
}

const s = StyleSheet.create({
  title: { ...t.display, color: colors.text, fontSize: 28, paddingHorizontal: 20, paddingVertical: 12 },
  emptyWrap: { flex: 1, alignItems: 'center', justifyContent: 'center', gap: 10, paddingBottom: 120 },
  emptyTitle: { ...t.display, color: colors.text, fontSize: 20 },
  emptySub: { color: colors.textDim, fontSize: 13 },
});
