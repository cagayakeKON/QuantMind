import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const transport = vi.hoisted(() => ({
  requests: [] as any[],
  handlers: [] as ((config: any) => any)[],
}));

vi.mock('axios', () => ({
  default: {
    create: () => ({
      interceptors: {
        request: { use: (handler: (config: any) => any) => transport.handlers.push(handler) },
        response: { use: vi.fn() },
      },
      post: async (url: string, data: unknown, options: any = {}) => {
        let config = { ...options, url, data, method: 'post' };
        for (const handler of transport.handlers) config = await handler(config);
        transport.requests.push(config);
        return { data: {
          access_token: 'test-access', refresh_token: 'test-refresh',
          token_type: 'bearer', expires_in: 60,
          user: { id: 7, username: 'routing-test', is_active: true },
        } };
      },
    }),
  },
}));

vi.mock('../../utils/performance', () => ({ performanceMonitor: { isMonitoring: false } }));
vi.mock('../../utils/errorHandler', () => ({
  handleError: vi.fn(), handleAuthError: vi.fn(),
  handleNetworkError: vi.fn(), handleServerError: vi.fn(),
}));

async function login() {
  const { authService } = await import('../authService');
  const result = await authService.login({
    email_or_username: 'routing-test', password: 'test-only-password',
  });
  expect(result.access_token).toBe('test-access');
  expect(localStorage.getItem('access_token')).toBe('test-access');
  expect(transport.requests).toHaveLength(1);
  return transport.requests[0];
}

beforeEach(() => {
  vi.resetModules();
  transport.requests.length = 0;
  transport.handlers.length = 0;
  localStorage.clear();
  delete (window as any).electronAPI;
  vi.stubEnv('VITE_DISABLE_AUTH', 'false');
  vi.stubEnv('VITE_USER_API_URL', '');
  vi.spyOn(console, 'log').mockImplementation(() => {});
});

afterEach(() => {
  delete (window as any).electronAPI;
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

describe('authentication request routing', () => {
  it('uses the Web proxy after the real compatibility shim loads', async () => {
    vi.stubEnv('VITE_USER_API_URL', 'http://127.0.0.1:8000');
    localStorage.setItem('quantmind_server_url_v2', 'http://stale.invalid:8000');
    await import('../../../../utils/electronCompat');
    const { isElectronEnv } = await import('../../../../config/services');
    expect(isElectronEnv()).toBe(false);
    const request = await login();
    expect(request.baseURL).toBe('');
    expect(request.url).toBe('/api/v1/auth/login');
  });

  it('uses the same relative route without a compatibility shim', async () => {
    const request = await login();
    expect(request.baseURL).toBe('');
    expect(request.url).toBe('/api/v1/auth/login');
  });

  it.each([
    ['http://127.0.0.1:8000', 'http://127.0.0.1:8000', '/api/v1/auth/login'],
    ['http://127.0.0.1:8000/api/v1/', 'http://127.0.0.1:8000', '/api/v1/auth/login'],
    ['https://auth.example.test/api/v2', 'https://auth.example.test', '/api/v2/auth/login'],
  ])('resolves the Electron authentication address %s', async (configured, origin, path) => {
    (window as any).electronAPI = {};
    vi.stubEnv('VITE_USER_API_URL', configured);
    const request = await login();
    expect(request.baseURL).toBe(origin);
    expect(request.url).toBe(path);
  });

  it('uses an updated desktop server and prefix after singleton creation', async () => {
    (window as any).electronAPI = {};
    await import('../authService');
    const { setDynamicServerUrl } = await import('../../../../config/services');
    setDynamicServerUrl('https://new.example.test/gateway');
    const request = await login();
    expect(request.baseURL).toBe('https://new.example.test');
    expect(request.url).toBe('/gateway/api/v1/auth/login');
  });
});
