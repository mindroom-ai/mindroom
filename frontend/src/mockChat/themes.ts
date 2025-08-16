import { ChatTheme, Platform } from './types';

const themes: Record<Platform, ChatTheme> = {
  slack: {
    platform: 'slack',
    backgroundColor: '#1a1d21',
    messageBackgroundSent: 'transparent',
    messageBackgroundReceived: 'transparent',
    textColorPrimary: '#d1d2d3',
    textColorSecondary: '#ababad',
    borderColor: '#2e3135',
    headerBackground: '#121317',
    fontFamily: 'Slack-Lato, Lato, -apple-system, sans-serif',
  },

  discord: {
    platform: 'discord',
    backgroundColor: '#36393f',
    messageBackgroundSent: 'transparent',
    messageBackgroundReceived: 'transparent',
    textColorPrimary: '#dcddde',
    textColorSecondary: '#72767d',
    borderColor: '#202225',
    headerBackground: '#2f3136',
    fontFamily: 'Whitney, "Helvetica Neue", Helvetica, Arial, sans-serif',
  },

  matrix: {
    platform: 'matrix',
    backgroundColor: '#f3f8fd',
    messageBackgroundSent: '#0dbd8b',
    messageBackgroundReceived: '#e7e7e7',
    textColorPrimary: '#2e2f32',
    textColorSecondary: '#737d8c',
    borderColor: '#e3e8f0',
    headerBackground: '#ffffff',
    fontFamily: 'Inter, -apple-system, BlinkMacSystemFont, sans-serif',
  },

  telegram: {
    platform: 'telegram',
    backgroundColor: '#0e1621',
    messageBackgroundSent: '#2b5278',
    messageBackgroundReceived: '#182533',
    textColorPrimary: '#ffffff',
    textColorSecondary: '#8b9ab5',
    borderColor: '#1c2733',
    headerBackground: '#17212b',
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
  },

  whatsapp: {
    platform: 'whatsapp',
    backgroundColor: '#0b141a',
    messageBackgroundSent: '#005c4b',
    messageBackgroundReceived: '#202c33',
    textColorPrimary: '#e9edef',
    textColorSecondary: '#8696a0',
    borderColor: '#2a3942',
    headerBackground: '#202c33',
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
  },
};

// Light theme variants
export const lightThemes: Partial<Record<Platform, ChatTheme>> = {
  slack: {
    platform: 'slack',
    backgroundColor: '#ffffff',
    messageBackgroundSent: 'transparent',
    messageBackgroundReceived: 'transparent',
    textColorPrimary: '#1d1c1d',
    textColorSecondary: '#616061',
    borderColor: '#dddddd',
    headerBackground: '#ffffff',
    fontFamily: 'Slack-Lato, Lato, -apple-system, sans-serif',
  },

  discord: {
    platform: 'discord',
    backgroundColor: '#ffffff',
    messageBackgroundSent: 'transparent',
    messageBackgroundReceived: 'transparent',
    textColorPrimary: '#2e3338',
    textColorSecondary: '#68727f',
    borderColor: '#ebedef',
    headerBackground: '#f2f3f5',
    fontFamily: 'Whitney, "Helvetica Neue", Helvetica, Arial, sans-serif',
  },

  whatsapp: {
    platform: 'whatsapp',
    backgroundColor: '#e5ddd5',
    messageBackgroundSent: '#dcf8c6',
    messageBackgroundReceived: '#ffffff',
    textColorPrimary: '#303030',
    textColorSecondary: '#667781',
    borderColor: '#d1d7db',
    headerBackground: '#00a884',
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
  },
};

export const getTheme = (platform: Platform, light = false): ChatTheme => {
  if (light && lightThemes[platform]) {
    return lightThemes[platform] as ChatTheme;
  }
  return themes[platform];
};
