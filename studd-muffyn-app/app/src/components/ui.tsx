// Shared UI primitives — gold buttons, badges, skeletons, section headers.
import React, { useEffect } from 'react';
import { Pressable, StyleSheet, Text, View, ViewStyle } from 'react-native';
import { LinearGradient } from 'expo-linear-gradient';
import * as Haptics from 'expo-haptics';
import Animated, {
  useAnimatedStyle,
  useSharedValue,
  withRepeat,
  withSpring,
  withTiming,
} from 'react-native-reanimated';
import { colors, goldGradient, radius, type as t } from '../theme';

export function GoldButton({
  label,
  onPress,
  style,
  small,
}: {
  label: string;
  onPress: () => void;
  style?: ViewStyle;
  small?: boolean;
}) {
  const scale = useSharedValue(1);
  const a = useAnimatedStyle(() => ({ transform: [{ scale: scale.value }] }));
  return (
    <Animated.View style={[a, style]}>
      <Pressable
        onPressIn={() => (scale.value = withSpring(0.96, { damping: 15 }))}
        onPressOut={() => (scale.value = withSpring(1, { damping: 12 }))}
        onPress={() => {
          Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Medium);
          onPress();
        }}
      >
        <LinearGradient
          colors={goldGradient}
          start={{ x: 0, y: 0 }}
          end={{ x: 1, y: 1 }}
          style={[s.goldBtn, small && { paddingVertical: 10 }]}
        >
          <Text style={[s.goldBtnLabel, small && { fontSize: 13 }]}>{label}</Text>
        </LinearGradient>
      </Pressable>
    </Animated.View>
  );
}

export function GhostButton({
  label,
  onPress,
  style,
}: {
  label: string;
  onPress: () => void;
  style?: ViewStyle;
}) {
  return (
    <Pressable onPress={onPress} style={[s.ghostBtn, style]}>
      <Text style={s.ghostLabel}>{label}</Text>
    </Pressable>
  );
}

export function Badge({ label, tone = 'gold' }: { label: string; tone?: 'gold' | 'dark' | 'danger' }) {
  const bg = tone === 'gold' ? colors.saleBadge : tone === 'danger' ? colors.saleBadge : '#888888';
  const fg = '#ffffff';
  return (
    <View style={[s.badge, { backgroundColor: bg }]}>
      <Text style={[s.badgeText, { color: fg }]}>{label}</Text>
    </View>
  );
}

export function SectionHeader({
  title,
  subtitle,
  actionLabel,
  onAction,
}: {
  title: string;
  subtitle?: string;
  actionLabel?: string;
  onAction?: () => void;
}) {
  return (
    <View style={s.sectionHead}>
      <View style={{ flex: 1 }}>
        <Text style={s.sectionTitle}>{title}</Text>
        {subtitle ? <Text style={s.sectionSub}>{subtitle}</Text> : null}
      </View>
      {actionLabel && onAction ? (
        <Pressable onPress={onAction} hitSlop={10}>
          <Text style={s.sectionAction}>{actionLabel} ›</Text>
        </Pressable>
      ) : null}
    </View>
  );
}

export function Skeleton({ style }: { style?: ViewStyle }) {
  const o = useSharedValue(0.35);
  useEffect(() => {
    o.value = withRepeat(withTiming(0.75, { duration: 700 }), -1, true);
  }, []);
  const a = useAnimatedStyle(() => ({ opacity: o.value }));
  return <Animated.View style={[{ backgroundColor: colors.surfaceHi, borderRadius: radius.md }, style, a]} />;
}

export function Divider() {
  return <View style={{ height: StyleSheet.hairlineWidth, backgroundColor: colors.line, marginVertical: 16 }} />;
}

const s = StyleSheet.create({
  goldBtn: {
    paddingVertical: 16,
    borderRadius: radius.pill,
    alignItems: 'center',
    justifyContent: 'center',
  },
  goldBtnLabel: { color: colors.cream, fontWeight: '800', fontSize: 15, letterSpacing: 0.8 },
  ghostBtn: {
    paddingVertical: 15,
    borderRadius: radius.pill,
    alignItems: 'center',
    borderWidth: 1,
    borderColor: colors.gold,
  },
  ghostLabel: { color: colors.gold, fontWeight: '700', fontSize: 14, letterSpacing: 0.6 },
  badge: {
    paddingHorizontal: 8,
    paddingVertical: 4,
    borderRadius: 7,
  },
  badgeText: { fontSize: 10, fontWeight: '800', letterSpacing: 0.4 },
  sectionHead: {
    flexDirection: 'row',
    alignItems: 'flex-end',
    paddingHorizontal: 20,
    marginTop: 34,
    marginBottom: 14,
  },
  sectionTitle: { ...t.display, color: colors.text, fontSize: 22 },
  sectionSub: { color: colors.textDim, fontSize: 12.5, marginTop: 3 },
  sectionAction: { color: colors.gold, fontSize: 13, fontWeight: '600' },
});
