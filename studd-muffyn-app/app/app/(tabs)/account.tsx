// Profile — orders/tracking/support open the real Shopify account pages
// (single source of truth stays Shopify).
import React from 'react';
import { Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { Ionicons } from '@expo/vector-icons';
import * as WebBrowser from 'expo-web-browser';
import { useRouter } from 'expo-router';
import { BASE } from '../../src/api/shopify';
import { useShop } from '../../src/store/shop';
import { colors, radius, type as t } from '../../src/theme';

const ROWS: { icon: any; label: string; url?: string; route?: string }[] = [
  { icon: 'person-circle-outline', label: 'Login / My Account', url: `${BASE}/account` },
  { icon: 'cube-outline', label: 'My Orders', url: `${BASE}/account` },
  { icon: 'navigate-outline', label: 'Track Order', url: `${BASE}/pages/track-order` },
  { icon: 'heart-outline', label: 'Wishlist', route: '/(tabs)/wishlist' },
  { icon: 'chatbubble-ellipses-outline', label: 'Ask Paras', url: `${BASE}/pages/ask-paras` },
  { icon: 'help-circle-outline', label: 'FAQ', url: `${BASE}/pages/faq` },
  { icon: 'star-outline', label: 'Media Coverage', url: `${BASE}/pages/products-rating-by-experts` },
  { icon: 'reader-outline', label: 'Blog', url: `${BASE}/blogs/paraskenuskhe-blog` },
  { icon: 'call-outline', label: 'Contact Us', url: `${BASE}/pages/contact-test` },
];

export default function Account() {
  const router = useRouter();
  const { recents, wishlist, cart } = useShop();
  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: colors.bg }} edges={['top']}>
      <Text style={s.title}>Profile</Text>
      <ScrollView contentContainerStyle={{ padding: 20, paddingBottom: 130 }}>
        <View style={s.statsRow}>
          <View style={s.stat}>
            <Text style={s.statNum}>{cart.length}</Text>
            <Text style={s.statLabel}>In bag</Text>
          </View>
          <View style={s.stat}>
            <Text style={s.statNum}>{wishlist.length}</Text>
            <Text style={s.statLabel}>Wishlisted</Text>
          </View>
          <View style={s.stat}>
            <Text style={s.statNum}>{recents.length}</Text>
            <Text style={s.statLabel}>Viewed</Text>
          </View>
        </View>

        {ROWS.map((r) => (
          <Pressable
            key={r.label}
            style={s.row}
            onPress={() => (r.route ? router.push(r.route as any) : WebBrowser.openBrowserAsync(r.url!))}
          >
            <Ionicons name={r.icon} size={20} color={colors.gold} />
            <Text style={s.rowText}>{r.label}</Text>
            <Ionicons name="chevron-forward" size={16} color={colors.textFaint} />
          </Pressable>
        ))}

        <Text style={s.foot}>Studd Muffyn · Powered by Shopify</Text>
      </ScrollView>
    </SafeAreaView>
  );
}

const s = StyleSheet.create({
  title: { ...t.display, color: colors.text, fontSize: 28, paddingHorizontal: 20, paddingVertical: 12 },
  statsRow: { flexDirection: 'row', gap: 12, marginBottom: 20 },
  stat: {
    flex: 1,
    backgroundColor: colors.surface,
    borderRadius: radius.lg,
    alignItems: 'center',
    paddingVertical: 16,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: colors.line,
  },
  statNum: { ...t.display, color: colors.gold, fontSize: 22, fontWeight: '700' },
  statLabel: { color: colors.textDim, fontSize: 11.5, marginTop: 4 },
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 14,
    paddingVertical: 17,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: colors.line,
  },
  rowText: { color: colors.text, fontSize: 14.5, flex: 1, fontWeight: '500' },
  foot: { color: colors.textFaint, fontSize: 11.5, textAlign: 'center', marginTop: 28 },
});
