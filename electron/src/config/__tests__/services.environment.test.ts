import { afterEach, beforeEach, expect, it, vi } from 'vitest';

beforeEach(() => {
  vi.resetModules();
  localStorage.clear();
  delete (window as any).electronAPI;
});

afterEach(() => {
  delete (window as any).electronAPI;
  vi.unstubAllEnvs();
});

it.each([undefined, null, { __quantmindWebCompat: true }])(
  'uses Web service endpoints for a non-native bridge %j', async (bridge) => {
    (window as any).electronAPI = bridge;
    vi.stubEnv('VITE_USER_API_URL', 'http://stale.invalid:8000');
    const services = await import('../services');
    expect(services.isElectronEnv()).toBe(false);
    expect(services.SERVICE_ENDPOINTS.USER_SERVICE).toBe('/api/v1');
    expect(services.resolveWebSafeServiceBase('http://stale.invalid', '/api/v1'))
      .toBe('/api/v1');
  },
);

it('preserves the native desktop bridge and configured service address', async () => {
  (window as any).electronAPI = { getPlatform: () => 'win32' };
  vi.stubEnv('VITE_USER_API_URL', 'https://desktop.example.test:8000');
  const services = await import('../services');
  expect(services.isElectronEnv()).toBe(true);
  expect(services.SERVICE_ENDPOINTS.USER_SERVICE)
    .toBe('https://desktop.example.test:8000/api/v1');
});
