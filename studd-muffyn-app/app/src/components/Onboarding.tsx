// First-launch onboarding — 3 swipe slides over real brand imagery.
import React, { useRef, useState } from 'react';
import { Dimensions, FlatList, Pressable, StyleSheet, Text, View } from 'react-native';
import { Image } from 'expo-image';
import { LinearGradient } from 'expo-linear-gradient';
import Animated, { FadeIn } from 'react-native-reanimated';
import { colors, type as t, SCREEN_W } from '../theme';
import { GoldButton } from './ui';

const W = SCREEN_W;
const H = Dimensions.get('window').height;

const SLIDES = [
  {
    image: 'https://studdmuffyn.com/cdn/shop/files/Studd_Muffyn_top_banner_-_2.jpg?v=1783515303&width=1400',
    title: 'Welcome to Studd Muffyn',
    sub: '24K gold plated jewellery, healing crystals, honest hair & skin care — all in one place.',
  },
  {
    image: 'https://studdmuffyn.com/cdn/shop/files/Banner-for-new-theme-BRACELETS_f386b349-049b-4573-a441-b46a5bacee44.jpg?v=1778307078&width=1400',
    title: 'Shop by Purpose',
    sub: 'Crystals curated for protection, love, career, abundance and calm.',
  },
  {
    image: 'https://studdmuffyn.com/cdn/shop/files/Studd_Muffyn_top_banner.jpg?v=1783515303&width=1400',
    title: 'Checkout you already trust',
    sub: 'Secure Shopify checkout with COD, coupons and fast nationwide shipping.',
  },
];

export function Onboarding({ onDone }: { onDone: () => void }) {
  const [page, setPage] = useState(0);
  const ref = useRef<FlatList>(null);
  const last = page === SLIDES.length - 1;
  return (
    <Animated.View entering={FadeIn.duration(300)} style={s.wrap}>
      <FlatList
        ref={ref}
        data={SLIDES}
        horizontal
        pagingEnabled
        showsHorizontalScrollIndicator={false}
        keyExtractor={(x) => x.title}
        onMomentumScrollEnd={(e) => setPage(Math.round(e.nativeEvent.contentOffset.x / W))}
        renderItem={({ item }) => (
          <View style={{ width: W, height: H }}>
            <Image source={{ uri: item.image }} style={s.img} contentFit="cover" transition={300} />
            <LinearGradient colors={['rgba(11,11,13,0.1)', 'rgba(11,11,13,0.96)']} style={StyleSheet.absoluteFill} />
            <View style={s.textWrap}>
              <Text style={s.title}>{item.title}</Text>
              <Text style={s.sub}>{item.sub}</Text>
            </View>
          </View>
        )}
      />
      <View style={s.footer}>
        <View style={s.dots}>
          {SLIDES.map((_, i) => (
            <View key={i} style={[s.dot, i === page && s.dotOn]} />
          ))}
        </View>
        <GoldButton
          label={last ? 'START SHOPPING' : 'NEXT'}
          onPress={() => {
            if (last) return onDone();
            ref.current?.scrollToIndex({ index: page + 1, animated: true });
            setPage(page + 1);
          }}
        />
        {!last && (
          <Pressable onPress={onDone} hitSlop={10}>
            <Text style={s.skip}>Skip</Text>
          </Pressable>
        )}
      </View>
    </Animated.View>
  );
}

const s = StyleSheet.create({
  wrap: { position: 'absolute', top: 0, left: 0, right: 0, bottom: 0, backgroundColor: colors.bg, zIndex: 100 },
  img: { width: W, height: H, position: 'absolute' },
  textWrap: { flex: 1, justifyContent: 'flex-end', padding: 28, paddingBottom: 190 },
  title: { ...t.display, color: colors.text, fontSize: 32, lineHeight: 40 },
  sub: { color: colors.textDim, fontSize: 15, lineHeight: 23, marginTop: 10 },
  footer: { position: 'absolute', left: 28, right: 28, bottom: 104, gap: 16 },
  dots: { flexDirection: 'row', gap: 6, justifyContent: 'center' },
  dot: { width: 6, height: 6, borderRadius: 3, backgroundColor: 'rgba(255,255,255,0.3)' },
  dotOn: { backgroundColor: colors.gold, width: 18 },
  skip: { color: colors.textFaint, textAlign: 'center', fontSize: 13 },
});
