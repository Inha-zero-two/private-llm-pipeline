interface ImportMetaEnv {
  /** mock(기본값): 브라우저 안 목업 / http: 실제 앱 서버 */
  readonly VITE_API_MODE?: 'mock' | 'http'
  /** 앱 서버 주소. 비우면 웹과 같은 주소의 /api */
  readonly VITE_API_BASE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
