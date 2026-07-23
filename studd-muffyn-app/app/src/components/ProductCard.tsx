// Premium product card — image, badges, wishlist heart, quick add.
import React from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';
import { Image } from 'expo-image';
import { useRouter } from 'expo-router';
import * as Haptics from 'expo-haptics';
import Animated, { FadeInDown, useAnimatedStyle, useSharedValue, withSpring } from 'react-native-reanimated';
import { Ionicons } from '@expo/vector-icons';
import type { Product } from '../api/types';
import { colors, INR, pctOff, radius, shadow, type as t } from '../theme';
import { useShop } from '../store/shop';
import { Badge } from './ui';

export function ProductCard({
  product,
  width = 168,
  index = 0,
}: {
  product: Product;
  width?: number;
  index?: number;
}) {
  const router = useRouter();
  const { wishlist, toggleWish, addToCart } = useShop();
  const wished = wishlist.includes(product.handle);
  const off = pctOff(product.price, product.compareAt);
  const scale = useSharedValue(1);
  const a = useAnimatedStyle(() => ({ transform: [{ scale: scale.value }] }));
  const soldOut = product.variants.length > 0 && product.variants.every((v) => !v.available);

  const quickAdd = () => {
    const v = product.variants.find((x) => x.available) ?? product.variants[0];
    if (!v) return;
    Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success);
    addToCart({
      handle: product.handle,
      variantId: v.id,
      variantTitle: v.title,
      title: product.title,
      image: product.images[0],
      price: v.price,
      compareAt: v.compareAt,
    });
  };

  return (
    <Animated.View entering={FadeInDown.delay(Math.min(index, 8) * 60).springify()} style={[a, { width }]}>
      <Pressable
        onPressIn={() => (scale.value = withSpring(0.97, { damping: 15 }))}
        onPressOut={() => (scale.value = withSpring(1, { damping: 12 }))}
        onPress={() => router.push(`/product/${product.handle}`)}
        style={[s.card, shadow.card]}
      >
        <View style={s.imageWrap}>
          <Image
            source={{ uri: product.images[0] }}
            style={s.image}
            contentFit="cover"
            transition={250}
            recyclingKey={product.handle}
          />
          <View style={s.badges}>
            {off > 0 && <Badge label={`${off}% OFF`} tone="gold" />}
            {soldOut && <Badge label="SOLD OUT" tone="dark" />}
          </View>
          <Pressable
            hitSlop={8}
            style={s.heart}
            onPress={() => {
              Haptics.selectionAsync();
              toggleWish(product.handle);
            }}
          >
            <Ionicons name={wished ? 'heart' : 'heart-outline'} size={18} color={wished ? colors.gold : '#fff'} />
          </Pressable>
        </View>
        <View style={s.info}>
          <Text numberOfLines={2} style={s.title}>
            {product.title}
          </Text>
          <View style={s.priceRow}>
            <Text style={[s.price, product.compareAt && product.compareAt > product.price ? { color: colors.sale } : null]}>{INR(product.price)}</Text>
            {product.compareAt && product.compareAt > product.price ? (
              <Text style={s.compare}>{INR(product.compareAt)}</Text>
            ) : null}
          </View>
        </View>
        {!soldOut && (
          <Pressable style={s.quickAdd} hitSlop={6} onPress={quickAdd}>
            <Ionicons name="add" size={18} color="#fff" />
          </Pressable>
        )}
      </Pressable>
    </Animated.View>
  );
}

const s = StyleSheet.create({
  card: {
    backgroundColor: colors.card,
    borderRadius: radius.lg,
    overflow: 'hidden',
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: colors.line,
  },
  imageWrap: { aspectRatio: 0.92, backgroundColor: colors.surfaceHi },
  image: { flex: 1 },
  badges: { position: 'absolute', top: 10, left: 10, gap: 6, flexDirection: 'row' },
  heart: {
    position: 'absolute',
    top: 8,
    right: 8,
    width: 32,
    height: 32,
    borderRadius: 16,
    backgroundColor: 'rgba(0,0,0,0.45)',
    alignItems: 'center',
    justifyContent: 'center',
  },
  info: { padding: 12, paddingBottom: 14 },
  title: { color: colors.text, fontSize: 13, lineHeight: 18, fontWeight: '500', minHeight: 36 },
  priceRow: { flexDirection: 'row', alignItems: 'baseline', gap: 7, marginTop: 7 },
  price: { ...t.display, color: colors.text, fontSize: 16, fontWeight: '700' },
  compare: { color: colors.textFaint, fontSize: 12, textDecorationLine: 'line-through' },
  quickAdd: {
    position: 'absolute',
    right: 10,
    bottom: 74,
    width: 32,
    height: 32,
    borderRadius: 16,
    backgroundColor: colors.gold,
    alignItems: 'center',
    justifyContent: 'center',
  },
});
