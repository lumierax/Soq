import type { CapacitorConfig } from '@capacitor/cli';

const config: CapacitorConfig = {
  appId: 'dev.nabd.app',
  appName: 'نبض',
  webDir: 'dist',
  backgroundColor: '#060b14',
  ios: {
    contentInset: 'never',
    preferredContentMode: 'mobile',
    scheme: 'App',
  },
};

export default config;
