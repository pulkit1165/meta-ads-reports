// Home — fully driven by src/config/home.json (merch dashboard-ready).
import React, { useEffect, useMemo, useState } from 'react';
import { Pressable, ScrollView, StyleSheet, View } from 'react-native';
import { Image } from 'expo-image';
import { useRouter } from 'expo-router';
import { SafeAreaView } from 'react-native-safe-area-context';
import { Ionicons } from '@expo/vector-icons';
import { useHomeConfig } from '../../src/api/remoteConfig';
import {
  AnnouncementTicker,
  BigBanner,
  CategoryGrid,
  Hero,
  IconRow,
  ImageBanner,
  LogoStrip,
  OfferCards,
  ProductRail,
  PurposeGrid,
} from '../../src/components/HomeSections';
import { getProduct } from '../../src/api/shopify';
import { Onboarding } from '../../src/components/Onboarding';
import { useShop } from '../../src/store/shop';
import { colors } from '../../src/theme';

function Header() {
  const router = useRouter();
  return (
    <View style={s.header}>
      <Image source={require('../../assets/logo.png')} style={s.logo} contentFit="contain" />
      <View style={{ flex: 1 }} />
      <Pressable hitSlop={10} onPress={() => router.push('/(tabs)/search')} style={s.hIcon}>
        <Ionicons name="search-outline" size={22} color={colors.text} />
      </Pressable>
      <Pressable hitSlop={10} onPress={() => router.push('/cart')} style={s.hIcon}>
        <Ionicons name="bag-handle-outline" size={22} color={colors.text} />
      </Pressable>
    </View>
  );
}

function RecentlyViewed({ title }: { title: string }) {
  const recents = useShop((st) => st.recents);
  const products = useMemo(
    () => recents.map((h) => getProduct(h)).filter(Boolean) as NonNullable<ReturnType<typeof getProduct>>[],
    [recents]
  );
  if (!products.length) return null;
  return <ProductRail title={title} products={products.slice(0, 12)} />;
}

export default function Home() {
  const cfg = useHomeConfig() as any;
  const onboarded = useShop((st) => st.onboarded);
  const setOnboarded = useShop((st) => st.setOnboarded);
  const [hydrated, setHydrated] = useState(() => useShop.persist.hasHydrated());
  useEffect(() => {
    if (useShop.persist.hasHydrated()) {
      setHydrated(true);
      return;
    }
    return useShop.persist.onFinishHydration(() => setHydrated(true));
  }, []);
  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: colors.bg }} edges={['top']}>
      <AnnouncementTicker messages={cfg.announcement.messages} />
      <Header />
      <ScrollView showsVerticalScrollIndicator={false} contentContainerStyle={{ paddingBottom: 130 }}>
        {cfg.sections.map((sec: any, i: number) => {
          switch (sec.type) {
            case 'hero':
              return <Hero key={i} slides={sec.slides} aspect={sec.aspect} />;
            case 'iconRow':
              return <IconRow key={i} items={sec.items} />;
            case 'logoStrip':
              return <LogoStrip key={i} images={sec.images} height={sec.height} />;
            case 'imageBanner':
              return <ImageBanner key={i} image={sec.image} url={sec.url} aspect={sec.aspect} />;
            case 'categoryGrid':
              return <CategoryGrid key={i} title={sec.title} items={sec.items} aspect={sec.aspect} showLabel={sec.showLabel !== false} />;
            case 'productRail':
              return <ProductRail key={i} title={sec.title} subtitle={sec.subtitle} handle={sec.handle} />;
            case 'offerCards':
              return <OfferCards key={i} title={sec.title} items={sec.items} />;
            case 'purposeGrid':
              return <PurposeGrid key={i} title={sec.title} subtitle={sec.subtitle} items={sec.items} />;
            case 'bigBanner':
              return <BigBanner key={i} title={sec.title} subtitle={sec.subtitle} handle={sec.handle} />;
            case 'recentlyViewed':
              return <RecentlyViewed key={i} title={sec.title} />;
            default:
              return null;
          }
        })}
      </ScrollView>
      {hydrated && !onboarded && <Onboarding onDone={setOnboarded} />}
    </SafeAreaView>
  );
}

const s = StyleSheet.create({
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    paddingHorizontal: 20,
    paddingVertical: 10,
    backgroundColor: colors.header,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: colors.line,
  },
  logo: { width: 150, height: 40 },
  hIcon: { marginLeft: 18 },
});
