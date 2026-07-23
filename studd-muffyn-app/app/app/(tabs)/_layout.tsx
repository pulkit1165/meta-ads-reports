import React from 'react';
import { Tabs, useRouter } from 'expo-router';
import { Pressable, StyleSheet, Text, View } from 'react-native';
import { BlurView } from 'expo-blur';
import { Ionicons } from '@expo/vector-icons';
import { colors } from '../../src/theme';
import { useShop, cartCount } from '../../src/store/shop';

function CartFab() {
  const router = useRouter();
  const count = useShop(cartCount);
  if (!count) return null;
  return (
    <Pressable style={s.fab} onPress={() => router.push('/cart')}>
      <Ionicons name="bag-handle" size={22} color={colors.cream} />
      <View style={s.fabBadge}>
        <Text style={s.fabBadgeText}>{count}</Text>
      </View>
    </Pressable>
  );
}

export default function TabsLayout() {
  return (
    <View style={{ flex: 1 }}>
      <Tabs
        screenOptions={{
          headerShown: false,
          tabBarActiveTintColor: colors.gold,
          tabBarInactiveTintColor: colors.textFaint,
          tabBarStyle: {
            position: 'absolute',
            borderTopColor: colors.line,
            backgroundColor: 'rgba(255,255,255,0.88)',
          },
          tabBarBackground: () => <BlurView intensity={40} tint="light" style={StyleSheet.absoluteFill} />,
          tabBarLabelStyle: { fontSize: 10.5, fontWeight: '600' },
        }}
      >
        <Tabs.Screen
          name="index"
          options={{
            title: 'Home',
            tabBarIcon: ({ color, size }) => <Ionicons name="home-outline" size={size} color={color} />,
          }}
        />
        <Tabs.Screen
          name="categories"
          options={{
            title: 'Shop',
            tabBarIcon: ({ color, size }) => <Ionicons name="grid-outline" size={size} color={color} />,
          }}
        />
        <Tabs.Screen
          name="search"
          options={{
            title: 'Search',
            tabBarIcon: ({ color, size }) => <Ionicons name="search-outline" size={size} color={color} />,
          }}
        />
        <Tabs.Screen
          name="wishlist"
          options={{
            title: 'Wishlist',
            tabBarIcon: ({ color, size }) => <Ionicons name="heart-outline" size={size} color={color} />,
          }}
        />
        <Tabs.Screen
          name="account"
          options={{
            title: 'Profile',
            tabBarIcon: ({ color, size }) => <Ionicons name="person-outline" size={size} color={color} />,
          }}
        />
      </Tabs>
      <CartFab />
    </View>
  );
}

const s = StyleSheet.create({
  fab: {
    position: 'absolute',
    right: 18,
    bottom: 104,
    width: 54,
    height: 54,
    borderRadius: 27,
    backgroundColor: colors.gold,
    alignItems: 'center',
    justifyContent: 'center',
    shadowColor: colors.gold,
    shadowOpacity: 0.5,
    shadowRadius: 14,
    shadowOffset: { width: 0, height: 4 },
    elevation: 10,
  },
  fabBadge: {
    position: 'absolute',
    top: -4,
    right: -4,
    minWidth: 20,
    height: 20,
    borderRadius: 10,
    backgroundColor: '#ffffff',
    borderWidth: 1.5,
    borderColor: colors.gold,
    alignItems: 'center',
    justifyContent: 'center',
    paddingHorizontal: 4,
  },
  fabBadgeText: { color: colors.gold, fontSize: 11, fontWeight: '800' },
});
