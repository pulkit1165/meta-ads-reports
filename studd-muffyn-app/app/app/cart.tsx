// Bag — savings summary, free-shipping progress, coupon field,
// checkout hands off to real Shopify checkout (cart permalink).
import React, { useState } from 'react';
import { FlatList, Pressable, StyleSheet, Text, TextInput, View } from 'react-native';
import { Image } from 'expo-image';
import { useRouter } from 'expo-router';
import { SafeAreaView, useSafeAreaInsets } from 'react-native-safe-area-context';
import { Ionicons } from '@expo/vector-icons';
import * as WebBrowser from 'expo-web-browser';
import * as Haptics from 'expo-haptics';
import { startCheckoutUrl } from '../src/api/shopify';
import { GoldButton } from '../src/components/ui';
import { cartSavings, cartTotal, useShop } from '../src/store/shop';
import { colors, INR, radius, type as t } from '../src/theme';

export default function Cart() {
  const router = useRouter();
  const insets = useSafeAreaInsets();
  const { cart, setQty, removeLine } = useShop();
  const total = useShop(cartTotal);
  const savings = useShop(cartSavings);
  const [coupon, setCoupon] = useState('');

  const checkout = async () => {
    if (!cart.length) return;
    Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Heavy);
    const url = await startCheckoutUrl(
      cart.map((l) => ({ variantId: l.variantId, qty: l.qty })),
      coupon.trim() || undefined
    );
    await WebBrowser.openBrowserAsync(url);
  };

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: colors.bg }} edges={['top']}>
      <View style={s.header}>
        <Text style={s.title}>Your Bag</Text>
        <Pressable hitSlop={12} onPress={() => router.back()}>
          <Ionicons name="close" size={26} color={colors.text} />
        </Pressable>
      </View>

      {cart.length === 0 ? (
        <View style={s.emptyWrap}>
          <Ionicons name="bag-handle-outline" size={44} color={colors.textFaint} />
          <Text style={s.emptyTitle}>Your bag is empty</Text>
          <Text style={s.emptySub}>Add something you love.</Text>
        </View>
      ) : (
        <>
          <FlatList
            data={cart}
            keyExtractor={(l) => String(l.variantId)}
            contentContainerStyle={{ padding: 20, paddingBottom: 20, gap: 14 }}
            renderItem={({ item }) => (
              <View style={s.line}>
                <Pressable onPress={() => router.push(`/product/${item.handle}`)}>
                  <Image source={{ uri: item.image }} style={s.lineImg} contentFit="cover" />
                </Pressable>
                <View style={{ flex: 1 }}>
                  <Text style={s.lineTitle} numberOfLines={2}>
                    {item.title}
                  </Text>
                  {item.variantTitle !== 'Default Title' && (
                    <Text style={s.lineVariant}>{item.variantTitle}</Text>
                  )}
                  <View style={s.lineBottom}>
                    <Text style={s.linePrice}>{INR(item.price * item.qty)}</Text>
                    <View style={s.stepper}>
                      <Pressable hitSlop={8} onPress={() => setQty(item.variantId, item.qty - 1)}>
                        <Ionicons name="remove" size={16} color={colors.gold} />
                      </Pressable>
                      <Text style={s.qty}>{item.qty}</Text>
                      <Pressable hitSlop={8} onPress={() => setQty(item.variantId, item.qty + 1)}>
                        <Ionicons name="add" size={16} color={colors.gold} />
                      </Pressable>
                    </View>
                  </View>
                </View>
                <Pressable hitSlop={10} style={s.remove} onPress={() => removeLine(item.variantId)}>
                  <Ionicons name="trash-outline" size={16} color={colors.textFaint} />
                </Pressable>
              </View>
            )}
          />

          <View style={[s.footer, { paddingBottom: insets.bottom + 14 }]}>
            <View style={s.shipRow}>
              <Text style={s.shipText}>✦ Free shipping on all prepaid orders</Text>
            </View>

            <View style={s.couponRow}>
              <TextInput
                value={coupon}
                onChangeText={setCoupon}
                placeholder="Coupon code (applied at checkout)"
                placeholderTextColor={colors.textFaint}
                autoCapitalize="characters"
                style={s.couponInput}
              />
            </View>

            {savings > 0 && (
              <View style={s.sumRow}>
                <Text style={s.sumLabel}>You're saving</Text>
                <Text style={[s.sumValue, { color: colors.success }]}>{INR(savings)}</Text>
              </View>
            )}
            <View style={s.sumRow}>
              <Text style={s.sumLabelBig}>Total</Text>
              <Text style={s.sumValueBig}>{INR(total)}</Text>
            </View>
            <GoldButton label="CHECKOUT SECURELY →" onPress={checkout} />
            <Text style={s.secureNote}>Secure checkout — UPI, cards & COD via Shiprocket / Shopify</Text>
          </View>
        </>
      )}
    </SafeAreaView>
  );
}

const s = StyleSheet.create({
  header: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', paddingHorizontal: 20, paddingVertical: 14 },
  title: { ...t.display, color: colors.text, fontSize: 26 },
  emptyWrap: { flex: 1, alignItems: 'center', justifyContent: 'center', gap: 10 },
  emptyTitle: { ...t.display, color: colors.text, fontSize: 20 },
  emptySub: { color: colors.textDim, fontSize: 13 },
  line: {
    flexDirection: 'row',
    gap: 12,
    backgroundColor: colors.card,
    borderRadius: radius.lg,
    padding: 12,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: colors.line,
  },
  lineImg: { width: 76, height: 76, borderRadius: radius.md, backgroundColor: colors.surfaceHi },
  lineTitle: { color: colors.text, fontSize: 13.5, fontWeight: '500', lineHeight: 18, paddingRight: 20 },
  lineVariant: { color: colors.textFaint, fontSize: 11.5, marginTop: 2 },
  lineBottom: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', marginTop: 10 },
  linePrice: { ...t.display, color: colors.gold, fontSize: 16, fontWeight: '700' },
  stepper: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 14,
    borderWidth: 1,
    borderColor: colors.line,
    borderRadius: radius.pill,
    paddingHorizontal: 12,
    paddingVertical: 6,
  },
  qty: { color: colors.text, fontSize: 13, fontWeight: '700', minWidth: 16, textAlign: 'center' },
  remove: { position: 'absolute', top: 12, right: 12 },
  footer: {
    paddingHorizontal: 20,
    paddingTop: 14,
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: colors.line,
    backgroundColor: colors.bg,
    gap: 10,
  },
  shipRow: { gap: 6 },
  shipText: { color: colors.goldSoft, fontSize: 12 },
  couponRow: { flexDirection: 'row', gap: 10 },
  couponInput: {
    flex: 1,
    height: 44,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colors.line,
    backgroundColor: colors.surface,
    color: colors.text,
    paddingHorizontal: 14,
    fontSize: 13,
  },
  sumRow: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center' },
  sumLabel: { color: colors.textDim, fontSize: 13 },
  sumValue: { fontSize: 14, fontWeight: '700' },
  sumLabelBig: { color: colors.text, fontSize: 16, fontWeight: '600' },
  sumValueBig: { ...t.display, color: colors.text, fontSize: 22, fontWeight: '700' },
  secureNote: { color: colors.textFaint, fontSize: 10.5, textAlign: 'center' },
});
