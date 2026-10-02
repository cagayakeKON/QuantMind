import axios from 'axios';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../utils/performance', () => ({ performanceMonitor: { isMonitoring: false } }));
vi.mock('../../utils/errorHandler', () => ({
  handleError: (error: unknown) => error,
  handleAuthError: (error: unknown) => error,
  handleNetworkError: (error: unknown) => error,
  handleServerError: (error: unknown) => error,
}));

beforeEach(() => {
  vi.resetModules();
  vi.stubEnv('VITE_DISABLE_AUTH', 'false');
  vi.stubEnv('VITE_USER_API_URL', '');
  localStorage.clear();
  delete (window as any).electronAPI;
  vi.spyOn(console, 'log').mockImplementation(() => {});
});

afterEach(() => {
  delete (window as any).electronAPI;
  localStorage.clear();
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

async function captureRequests() {
  const { authService } = await import('../authService');
  const urls: string[] = [];
  const user = { id: 1, username: 'test-user', email: 'test@example.com' };
  (authService as any).axiosInstance.defaults.adapter = async (config: any) => {
    urls.push(axios.getUri(config));
    return {
      config, status: 200, statusText: 'OK', headers: {},
      data: config.url.endsWith('/users/me') ? user : {
        access_token: 'test.token.signature', refresh_token: 'test-refresh', token_type: 'bearer', user,
      },
    };
  };
  return { authService, urls };
}

async function loginAndReadUser(service: Awaited<ReturnType<typeof captureRequests>>) {
  await service.authService.login({ email_or_username: 'test-user', password: 'test-password' });
  await service.authService.getCurrentUser();
  return service.urls;
}

describe('authentication request routing', () => {
  it('keeps Web login and current-user requests on one API prefix', async () => {
    vi.stubEnv('VITE_USER_API_URL', 'http://build-machine:8000/api/v1');
    expect(await loginAndReadUser(await captureRequests())).toEqual([
      '/api/v1/auth/login', '/api/v1/users/me',
    ]);
  }, 15000);

  it('keeps Web routing when the compatibility shim appears after service construction', async () => {
    const service = await captureRequests();
    (window as any).electronAPI = { __quantmindWebCompat: true };
    const { isElectronEnv, setDynamicServerUrl } = await import('../../../../config/services');
    setDynamicServerUrl('http://saved-desktop-server:8000');
    expect(isElectronEnv()).toBe(false);
    expect(await loginAndReadUser(service)).toEqual(['/api/v1/auth/login', '/api/v1/users/me']);
  });

  it('uses the native Electron server and follows server changes', async () => {
    (window as any).electronAPI = {};
    const { isElectronEnv, setDynamicServerUrl } = await import('../../../../config/services');
    expect(isElectronEnv()).toBe(true);
    setDynamicServerUrl('http://first-server:8000');
    const service = await captureRequests();
    await service.authService.login({ email_or_username: 'test-user', password: 'test-password' });
    setDynamicServerUrl('http://second-server:8000');
    await service.authService.getCurrentUser();
    expect(service.urls).toEqual([
      'http://first-server:8000/api/v1/auth/login', 'http://second-server:8000/api/v1/users/me',
    ]);
  });

  it.each(['http://configured-server:8000', 'http://configured-server:8000/api/v1']) (
    'accepts the Electron user-service setting %s', async (base) => {
      (window as any).electronAPI = {};
      vi.stubEnv('VITE_USER_API_URL', base);
      expect(await loginAndReadUser(await captureRequests())).toEqual([
        'http://configured-server:8000/api/v1/auth/login',
        'http://configured-server:8000/api/v1/users/me',
      ]);
    },
  );
});
