// Shop tab — the site's full navigation tree, rendered as an elegant browser.
import React, { useMemo, useState } from 'react';
import { Pressable, ScrollView, SectionList, StyleSheet, Text, View } from 'react-native';
import { Image } from 'expo-image';
import { useRouter } from 'expo-router';
import { SafeAreaView } from 'react-native-safe-area-context';
import { Ionicons } from '@expo/vector-icons';
import { collectionProducts, getCollection, nav } from '../../src/api/shopify';
import { colors, radius, type as t } from '../../src/theme';

// Curated top-level departments mapped onto the site's real nav/collections.
const DEPARTMENTS: { title: string; items: { title: string; url: string }[] }[] = [
  {
    title: 'Jewellery',
    items: [
      { title: 'All Gold Plated Jewellery', url: '/collections/24k-gold-plated-jewellery' },
      { title: 'Buy 2 Chains @ ₹699', url: '/collections/buy-1-get-1-free' },
      { title: 'Gold Plated Chain @ ₹399', url: '/collections/gold-plated-chain' },
      { title: 'Gold Plated Bracelet', url: '/collections/gold-plated-bracelet' },
      { title: 'Gold Plated Pendant', url: '/collections/gold-plated-pendant' },
      { title: '1 Pendant + 1 Chain @ ₹499', url: '/collections/golden-charms-collection' },
    ],
  },
  {
    title: 'Crystals',
    items: [
      { title: 'All Crystals', url: '/collections/crystals' },
      { title: 'Raw Crystals', url: '/collections/raw-crystals' },
      { title: 'Bracelets', url: '/collections/bracelets-1' },
      { title: 'Earrings', url: '/collections/earrings' },
      { title: 'Rings', url: '/collections/rings' },
      { title: 'Pendants', url: '/collections/pendent' },
      { title: 'Healing Kits', url: '/collections/healing-kits' },
      { title: 'Money Bowl', url: '/collections/money-bowl-1' },
    ],
  },
  {
    title: 'Crystal Home Decor',
    items: [
      { title: 'All Crystal Decor', url: '/collections/crystal-home-decor-collection' },
      { title: 'Frames & Wall Art', url: '/collections/frames-wall-art' },
      { title: 'Crystal Safari', url: '/collections/studd-muffyn-crystal-safari' },
      { title: 'Crystal Canvas', url: '/collections/studd-muffyn-crystal-canvas' },
      { title: 'Crystal Clock', url: '/collections/pyrite-clock' },
      { title: 'Wishlist Vase', url: '/collections/crystal-wishlist-vase' },
      { title: 'God Figures', url: '/collections/god-figures' },
      { title: 'Miniatures', url: '/collections/miniatures' },
      { title: 'Raw Geode', url: '/collections/raw-geode' },
      { title: 'Table Decor', url: '/collections/table-decor-1' },
      { title: 'Office Decor', url: '/collections/office-decor' },
      { title: 'Animal Sculptures', url: '/collections/animal-sculptures' },
      { title: 'Planters', url: '/collections/planters' },
    ],
  },
  {
    title: 'Shop by Purpose',
    items: [
      { title: 'Protection & Positivity', url: '/collections/protection-positivity' },
      { title: 'Love & Relationships', url: '/collections/love-relationships' },
      { title: 'Focus & Career', url: '/collections/focus-career' },
      { title: 'Luck & Abundance', url: '/collections/luck-abundance' },
      { title: 'Healing & Calm', url: '/collections/healing-calm' },
      { title: 'Spiritual Growth', url: '/collections/spiritual-growth' },
      { title: 'Meditation & Chakras', url: '/collections/meditation-chakras' },
      { title: 'Gifting & New Beginnings', url: '/collections/gifting-new-beginnings' },
    ],
  },
  {
    title: 'Hair Care',
    items: [
      { title: 'Bestsellers', url: '/collections/hair-care-bestsellers' },
      { title: 'All Hair Care', url: '/collections/hair-care' },
      { title: 'Hair Growth', url: '/collections/hair-growth' },
      { title: 'Dandruff Control', url: '/collections/dandruff-control' },
      { title: 'Fermented Rice Kit', url: '/collections/fermented-rice-kit' },
      { title: 'Conditioner & Shampoo', url: '/collections/conditioner-shampoo' },
      { title: 'Oiling — Sunday Wali Champi', url: '/collections/oiling-sunday-wali-champi' },
      { title: 'Phuss Phuss Mist', url: '/collections/phuss-phuss-mist' },
    ],
  },
  {
    title: 'Skin Care',
    items: [
      { title: 'Bestsellers', url: '/collections/skin-care-bestsellers' },
      { title: 'All Skin Care', url: '/collections/all-skin-care-1' },
      { title: 'Pigmentation', url: '/collections/pigmentation' },
      { title: 'Anti Aging', url: '/collections/time-reversal-for-lines-wrinkles-facial-elasticity' },
      { title: 'Glow', url: '/collections/glow' },
      { title: 'Acne', url: '/collections/acne' },
      { title: 'Dark Circles', url: '/collections/dark-circles' },
      { title: 'Face Wash', url: '/collections/face-wash' },
      { title: 'Skin Serum', url: '/collections/skin-serum' },
      { title: 'Face Mist', url: '/collections/face-mist' },
      { title: 'Face Masks', url: '/collections/face-mask' },
      { title: 'Lip Care', url: '/collections/lip' },
      { title: 'Sun Protection', url: '/collections/sun-protection-1' },
      { title: 'Tan Removal', url: '/collections/tan-removal' },
      { title: 'Night Skin Care', url: '/collections/night-skin-care' },
      { title: 'BB Cream', url: '/collections/bb-cream' },
    ],
  },
  {
    title: 'Perfumes',
    items: [
      { title: 'Best Sellers', url: '/collections/perfume-best-sellers' },
      { title: '10ml EDP', url: '/collections/10ml-edp' },
      { title: 'Perfume Pack of 6', url: '/collections/perfume-pack-of-6' },
    ],
  },
  {
    title: 'Gifting',
    items: [
      { title: 'Gifting Collection', url: '/collections/crystal-gifting' },
      { title: 'Birthday Gifting', url: '/collections/birthday-gifting-range' },
      { title: 'Anniversary Gifting', url: '/collections/anniversary-gifting-range' },
      { title: 'New Beginnings', url: '/collections/new-beginnings' },
      { title: 'For Brother', url: '/collections/gifting-for-brother' },
      { title: 'For Sister', url: '/collections/gifting-for-sister' },
      { title: 'For Mother', url: '/collections/gifting-for-mother' },
      { title: 'For Father', url: '/collections/gifting-for-father' },
    ],
  },
  {
    title: 'Combos & Offers',
    items: [
      { title: 'Money Saving Combos', url: '/collections/nuskhe-combos' },
      { title: 'Combos under ₹999', url: '/collections/combos-under-999' },
      { title: 'Hair Care Combos', url: '/collections/hair-pack-of-2' },
      { title: 'Skin Care Combos', url: '/collections/skin-pack-of-2' },
      { title: 'Crystal Combos', url: '/collections/crystal-combo' },
    ],
  },
  {
    title: 'More',
    items: [
      { title: 'Best Sellers', url: '/collections/best-seller-2025' },
      { title: 'New Launches', url: '/collections/studd-muffyn-new-launches' },
      { title: 'Nutraceuticals', url: '/collections/nutraceuticals' },
      { title: 'Makeup Care', url: '/collections/makeup-care' },
    ],
  },
];

export default function Categories() {
  const router = useRouter();
  const [active, setActive] = useState(0);
  const dept = DEPARTMENTS[active];

  const heroImage = (url: string) => {
    const handle = url.split('/collections/')[1];
    const c = handle ? getCollection(handle) : undefined;
    return c?.image || (handle ? collectionProducts(handle)[0]?.images[0] : undefined);
  };

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: colors.bg }} edges={['top']}>
      <Text style={s.title}>Shop</Text>
      <View style={{ flex: 1, flexDirection: 'row' }}>
        <ScrollView style={s.railCol} showsVerticalScrollIndicator={false}>
          {DEPARTMENTS.map((d, i) => (
            <Pressable key={d.title} style={[s.deptBtn, i === active && s.deptActive]} onPress={() => setActive(i)}>
              <Text style={[s.deptText, i === active && s.deptTextActive]}>{d.title}</Text>
            </Pressable>
          ))}
          <View style={{ height: 120 }} />
        </ScrollView>
        <ScrollView style={{ flex: 1 }} showsVerticalScrollIndicator={false} contentContainerStyle={{ padding: 14, paddingBottom: 130 }}>
          {dept.items.map((it) => {
            const handle = it.url.split('/collections/')[1];
            const img = heroImage(it.url);
            return (
              <Pressable key={it.url} style={s.itemRow} onPress={() => handle && router.push(`/collection/${handle}`)}>
                <Image source={{ uri: img }} style={s.itemImg} contentFit="cover" transition={200} />
                <Text style={s.itemText} numberOfLines={2}>
                  {it.title}
                </Text>
                <Ionicons name="chevron-forward" size={16} color={colors.textFaint} />
              </Pressable>
            );
          })}
        </ScrollView>
      </View>
    </SafeAreaView>
  );
}

const s = StyleSheet.create({
  title: { ...t.display, color: colors.text, fontSize: 28, paddingHorizontal: 20, paddingVertical: 12 },
  railCol: { width: 118, borderRightWidth: StyleSheet.hairlineWidth, borderRightColor: colors.line },
  deptBtn: { paddingVertical: 16, paddingHorizontal: 14 },
  deptActive: { backgroundColor: colors.surface, borderLeftWidth: 2, borderLeftColor: colors.gold },
  deptText: { color: colors.textDim, fontSize: 12.5, fontWeight: '600' },
  deptTextActive: { color: colors.gold },
  itemRow: {
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: colors.card,
    borderRadius: radius.md,
    padding: 10,
    marginBottom: 10,
    gap: 12,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: colors.line,
  },
  itemImg: { width: 52, height: 52, borderRadius: radius.sm, backgroundColor: colors.surfaceHi },
  itemText: { color: colors.text, fontSize: 13.5, flex: 1, fontWeight: '500' },
});
