// Instant search — as-you-type results over the full catalog,
// search history + trending shortcuts.
import React, { useMemo, useState } from 'react';
import { Dimensions, FlatList, Pressable, ScrollView, StyleSheet, Text, TextInput, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { Ionicons } from '@expo/vector-icons';
import { searchProducts } from '../../src/api/shopify';
import { ProductCard } from '../../src/components/ProductCard';
import { useShop } from '../../src/store/shop';
import { colors, radius, type as t, SCREEN_W } from '../../src/theme';

const W = SCREEN_W;
const CARD_W = (W - 52) / 2;

const TRENDING = [
  'rice water',
  'gold chain',
  'pyrite',
  'pigmentation',
  'perfume',
  'bracelet',
  'hair growth',
  'money bowl',
];

export default function Search() {
  const [q, setQ] = useState('');
  const { searches, pushSearch } = useShop();
  const results = useMemo(() => searchProducts(q), [q]);

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: colors.bg }} edges={['top']}>
      <Text style={s.title}>Search</Text>
      <View style={s.searchBar}>
        <Ionicons name="search" size={18} color={colors.textFaint} />
        <TextInput
          value={q}
          onChangeText={setQ}
          onSubmitEditing={() => q.trim() && pushSearch(q.trim())}
          placeholder="Search products, crystals, serums…"
          placeholderTextColor={colors.textFaint}
          style={s.input}
          autoCorrect={false}
          returnKeyType="search"
        />
        {q.length > 0 && (
          <Pressable hitSlop={10} onPress={() => setQ('')}>
            <Ionicons name="close-circle" size={18} color={colors.textFaint} />
          </Pressable>
        )}
      </View>

      {q.trim().length === 0 ? (
        <ScrollView contentContainerStyle={{ padding: 20, paddingBottom: 130 }}>
          {searches.length > 0 && (
            <>
              <Text style={s.groupTitle}>Recent</Text>
              <View style={s.chipWrap}>
                {searches.map((x) => (
                  <Pressable key={x} style={s.chip} onPress={() => setQ(x)}>
                    <Ionicons name="time-outline" size={13} color={colors.textDim} />
                    <Text style={s.chipText}>{x}</Text>
                  </Pressable>
                ))}
              </View>
            </>
          )}
          <Text style={s.groupTitle}>Trending</Text>
          <View style={s.chipWrap}>
            {TRENDING.map((x) => (
              <Pressable key={x} style={s.chip} onPress={() => setQ(x)}>
                <Ionicons name="trending-up-outline" size={13} color={colors.gold} />
                <Text style={s.chipText}>{x}</Text>
              </Pressable>
            ))}
          </View>
        </ScrollView>
      ) : (
        <FlatList
          data={results}
          numColumns={2}
          keyExtractor={(p) => p.handle}
          keyboardShouldPersistTaps="handled"
          columnWrapperStyle={{ gap: 12, paddingHorizontal: 20 }}
          contentContainerStyle={{ gap: 12, paddingTop: 10, paddingBottom: 130 }}
          renderItem={({ item, index }) => <ProductCard product={item} width={CARD_W} index={index % 6} />}
          ListEmptyComponent={<Text style={s.empty}>No products found for “{q}”.</Text>}
        />
      )}
    </SafeAreaView>
  );
}

const s = StyleSheet.create({
  title: { ...t.display, color: colors.text, fontSize: 28, paddingHorizontal: 20, paddingVertical: 12 },
  searchBar: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 10,
    marginHorizontal: 20,
    paddingHorizontal: 16,
    height: 48,
    borderRadius: radius.pill,
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderColor: colors.line,
  },
  input: { flex: 1, color: colors.text, fontSize: 15 },
  groupTitle: { color: colors.textDim, fontSize: 12, fontWeight: '800', letterSpacing: 1, marginTop: 18, marginBottom: 10 },
  chipWrap: { flexDirection: 'row', flexWrap: 'wrap', gap: 8 },
  chip: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 6,
    paddingHorizontal: 13,
    paddingVertical: 9,
    borderRadius: radius.pill,
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderColor: colors.line,
  },
  chipText: { color: colors.text, fontSize: 13 },
  empty: { color: colors.textDim, textAlign: 'center', marginTop: 60 },
});
