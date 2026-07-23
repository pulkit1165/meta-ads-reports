import React from 'react';
import { Dimensions, Platform, StyleSheet, View } from 'react-native';
import { GestureHandlerRootView } from 'react-native-gesture-handler';
import { Stack } from 'expo-router';
import { StatusBar } from 'expo-status-bar';
import { colors, SCREEN_W } from '../src/theme';

function AppStack() {
  return (
    <GestureHandlerRootView style={{ flex: 1, backgroundColor: colors.bg }}>
      <StatusBar style="dark" />
      <Stack
        screenOptions={{
          headerShown: false,
          contentStyle: { backgroundColor: colors.bg },
          animation: 'slide_from_right',
        }}
      >
        <Stack.Screen name="(tabs)" />
        <Stack.Screen name="cart" options={{ presentation: 'modal', animation: 'slide_from_bottom' }} />
      </Stack>
    </GestureHandlerRootView>
  );
}

export default function RootLayout() {
  // On web, render the app inside a centered phone-width frame so a
  // desktop browser shows the mobile experience, not a stretched page.
  if (Platform.OS === 'web') {
    return (
      <View style={s.backdrop}>
        <View style={s.phone}>
          <AppStack />
        </View>
      </View>
    );
  }
  return <AppStack />;
}

const s = StyleSheet.create({
  backdrop: {
    flex: 1,
    backgroundColor: '#171310',
    alignItems: 'center',
    justifyContent: 'center',
  },
  phone: {
    flex: 1,
    width: '100%',
    maxWidth: SCREEN_W,
    backgroundColor: colors.bg,
    overflow: 'hidden',
    // Gold edge only when the frame is narrower than the window (desktop);
    // on phones the frame IS the screen and borders would eat layout width.
    ...(Dimensions.get('window').width > 430
      ? {
          borderLeftWidth: StyleSheet.hairlineWidth,
          borderRightWidth: StyleSheet.hairlineWidth,
          borderColor: 'rgba(212,175,55,0.25)',
        }
      : null),
  },
});
