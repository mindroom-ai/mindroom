/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_URL: string;
  readonly VITE_MINDROOM_PORT: string;
  readonly VITE_PLATFORM_URL: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
