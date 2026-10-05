import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { AdminTrainingDatasets } from '../AdminTrainingDatasets';
import { AdminModelManagement } from '../AdminModelManagement';
import { AdminStrategyTemplates } from '../AdminStrategyTemplates';
import { ModelHubPage } from '../../../../pages/ModelHubPage';

const http = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn() }));
// Keep the production AdminService request builders; replace only HTTP transport.
vi.mock('axios', () => ({ default: { create: () => ({ ...http,
  interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
}) } }));
vi.mock('../../../auth/services/authService', () => ({ authService: { getAccessToken: () => null } }));
vi.mock('react-router-dom', () => ({ useNavigate: () => vi.fn() }));
vi.mock('react-redux', () => ({ useDispatch: () => vi.fn() }));
vi.mock('@monaco-editor/react', () => ({ default: () => <div /> }));
vi.mock('../../../../services/modelTrainingService', () => ({ modelTrainingService: {
  listUserModels: async () => ({ items: [] }),
} }));

const mapping = { mapping_id: 'field-1', source_dataset: 'l1_factors', source_column: 'momentum_5',
  key: 'momentum_5', feature_name: '五日动量', enabled: true, default_selected: true, required: false,
  category_id: 'momentum', category_name: '动量' };

beforeEach(() => { vi.clearAllMocks(); });

async function chooseMarket(label: string) {
  fireEvent.mouseDown(screen.getAllByRole('combobox')[0]);
  fireEvent.click(await screen.findByText(label, { selector: '.ant-select-item-option-content' }));
}

function trainingTransport(initialPublished = false) {
  let published = initialPublished;
  const version = (id: string, market: string) => ({ version_id: id, version_name: `${market} 因子集`,
    feature_count: 1, market, categories: [{ features: [mapping] }] });
  http.get.mockImplementation(async (url: string, options: any) => {
    const market = options.params.market;
    if (url.endsWith('/sources')) return { data: { sources: { l1_factors: { ready: true, files: 1 } },
      labels: { l1_factors: 'L1 因子（默认）' }, default_source: 'l1_factors' } };
    if (url.endsWith('/fields')) return { data: { fields: [{ column_name: 'momentum_5', is_present: true }] } };
    if (url.endsWith('/catalog')) {
      const id = options.params.version_id;
      if (id?.startsWith('JP-') && market !== 'JP') throw new Error('Catalog version belongs to a different market');
      return { data: id ? version(id, market) : published && market === 'JP' ? version('JP-published', market) : { catalog: null } };
    }
    throw new Error(`Unexpected GET ${url}`);
  });
  http.post.mockImplementation(async (url: string, _body: any, options: any) => {
    if (url === '/admin/training-data/versions') return { data: { version_id: `${options.params.market}-draft` } };
    if (url.endsWith('/clone')) return { data: { version_id: 'JP-clone' } };
    if (url.endsWith('/publish')) published = true;
    return { data: { default_selected_fields: 1 } };
  });
  http.put.mockResolvedValue({ data: {} });
}

describe('registered Japan market in shared admin pages', () => {
  it('discovers, creates, edits and publishes JP factor mappings through public requests', async () => {
    trainingTransport();
    render(<AdminTrainingDatasets />);
    await screen.findByText('momentum_5');
    await chooseMarket('日股');
    await waitFor(() => expect(http.get).toHaveBeenCalledWith('/admin/training-data/fields', {
      params: { source_dataset: 'l1_factors', market: 'JP' },
    }));
    fireEvent.click(screen.getByRole('button', { name: /字段发现/ }));
    await waitFor(() => expect(http.post).toHaveBeenCalledWith('/admin/training-data/sources/refresh',
      undefined, { params: { market: 'JP' }, timeout: 120000 }));
    fireEvent.change(screen.getByPlaceholderText('例如：2026-08 默认因子集'), { target: { value: 'JP 因子集' } });
    fireEvent.click(screen.getByRole('button', { name: /新建草稿/ }));
    await screen.findByRole('button', { name: /发布此草稿/ });
    expect(http.post).toHaveBeenCalledWith('/admin/training-data/versions',
      { version_name: 'JP 因子集', source_dataset: 'l1_factors' }, { params: { market: 'JP' } });
    fireEvent.click(screen.getAllByRole('switch')[0]);
    await waitFor(() => expect(http.put).toHaveBeenCalledWith('/admin/training-data/versions/JP-draft/mappings',
      expect.objectContaining({ mapping: expect.objectContaining({ enabled: false }) })));
    await waitFor(() => expect(http.get).toHaveBeenLastCalledWith('/admin/training-data/catalog', {
      params: { source_dataset: 'l1_factors', version_id: 'JP-draft', market: 'JP' },
    }));
    fireEvent.click(screen.getByRole('button', { name: /发布此草稿/ }));
    await waitFor(() => expect(http.post).toHaveBeenCalledWith('/admin/training-data/versions/JP-draft/publish'));
  }, 15000);

  it('clones the published JP version and reloads its draft with the same market', async () => {
    trainingTransport(true);
    render(<AdminTrainingDatasets />);
    await screen.findByText('momentum_5');
    await chooseMarket('日股');
    await screen.findByRole('button', { name: '复制为草稿' });
    fireEvent.click(screen.getByRole('button', { name: '复制为草稿' }));
    await screen.findByRole('button', { name: /发布此草稿/ });
    expect(http.get).toHaveBeenCalledWith('/admin/training-data/catalog', {
      params: { source_dataset: 'l1_factors', version_id: 'JP-clone', market: 'JP' },
    });
  });

  it.each([['CN', 'A股'], ['HK', '港股'], ['US', '美股']])('preserves %s source discovery requests', async (market, label) => {
    trainingTransport();
    render(<AdminTrainingDatasets />);
    await screen.findByText('momentum_5');
    if (market !== 'CN') await chooseMarket(label);
    await waitFor(() => expect(http.get).toHaveBeenCalledWith('/admin/training-data/fields', {
      params: { source_dataset: 'l1_factors', market },
    }));
    fireEvent.click(screen.getByRole('button', { name: /字段发现/ }));
    await waitFor(() => expect(http.post).toHaveBeenCalledWith('/admin/training-data/sources/refresh',
      undefined, { params: { market }, timeout: 120000 }));
  });

  it('labels and filters JP metadata, workflow and Qlib models without changing legacy classifications', async () => {
    const specs = [
      ['jp-meta', { metadata: { market: 'JP' } }], ['jp-adapter', { metadata: { market: 'japan' } }],
      ['jp-workflow', { workflow_config: { market: 'JP' } }], ['jp-qlib', { qlib_config: { market: 'japan' } }],
      ['jp-context', { metadata: { context: { market: 'JP' } }, workflow_config: { market: 'CN' } }],
      ['cn-model', { metadata: { market: 'CN' } }], ['hk-model', { metadata: { market: 'HK' } }],
      ['us-model', { metadata: { market: 'US' } }], ['default-model', {}],
    ];
    const models = specs.map(([model_id, context]) => ({ model_id, dir_path: `/fixture/${model_id}`,
      is_production: false, files: [], updated_at: '2026-10-05T00:00:00Z', ...context as object }));
    http.get.mockResolvedValue({ data: { total: models.length, models } });
    render(<AdminModelManagement />);
    await screen.findByText('jp-meta');
    for (const id of ['jp-meta', 'jp-adapter', 'jp-workflow', 'jp-qlib', 'jp-context']) {
      expect(within(screen.getByText(id).closest('tr')!).getByText('日股')).toBeTruthy();
    }
    fireEvent.click(screen.getByRole('radio', { name: '日股' }));
    expect(screen.getByText('jp-meta')).toBeTruthy();
    expect(screen.queryByText('cn-model')).toBeNull();
    for (const [label, ids] of [['A股', ['cn-model', 'default-model']], ['港股', ['hk-model']], ['美股', ['us-model']]] as const) {
      fireEvent.click(screen.getByRole('radio', { name: label }));
      ids.forEach(id => expect(screen.getByText(id)).toBeTruthy());
      expect(screen.queryByText('jp-meta')).toBeNull();
    }
    expect(http.get).toHaveBeenCalledWith('/admin/models/scan', expect.objectContaining({ params: { refresh: false } }));
  });

  it.each([false, true])('saves JP template applicability and preserves all-markets empty list (all=%s)', async (allMarkets) => {
    const template = { id: 'fixture', name: '跨市场 Top-K', description: '普通多市场模板', category: 'basic',
      difficulty: 'beginner', code: 'STRATEGY_CONFIG = {}', params: [], markets: ['a_share'] };
    http.get.mockResolvedValue({ data: { templates: [template], total: 1 } });
    http.put.mockImplementation(async (_url: string, payload: any) => {
      template.markets = payload.markets;
      return { data: { success: true } };
    });
    render(<AdminStrategyTemplates />);
    fireEvent.click(await screen.findByRole('button', { name: /编辑/ }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('checkbox', { name: '日股' }));
    if (allMarkets) {
      for (const label of ['港股', '美股', '加密']) fireEvent.click(within(dialog).getByRole('checkbox', { name: label }));
    } else fireEvent.click(within(dialog).getByRole('checkbox', { name: 'A股' }));
    fireEvent.click(within(dialog).getByRole('button', { name: /保存更新/ }));
    await waitFor(() => expect(http.put).toHaveBeenCalledWith('/admin/strategy-templates/fixture',
      expect.objectContaining({ markets: allMarkets ? [] : ['japan'], code: template.code })));
    await waitFor(() => expect(within(screen.getByText('跨市场 Top-K').closest('tr')!).getByText('日股')).toBeTruthy());
  });

  it('filters community JP models through the existing public hub request and preserves legacy filters', async () => {
    http.get.mockResolvedValue({ data: { items: [], total: 0 } });
    render(<ModelHubPage />);
    await waitFor(() => expect(http.get).toHaveBeenCalledWith('/api/v1/hub/models', expect.objectContaining({
      params: expect.objectContaining({ market: undefined, page: 1 }),
    })));
    for (const [market, label] of [['JP', '日股市场'], ['CN', 'A股市场'], ['HK', '港股市场'], ['US', '美股市场']]) {
      await chooseMarket(label);
      await waitFor(() => expect(http.get).toHaveBeenLastCalledWith('/api/v1/hub/models', {
        params: { market, algorithm: undefined, sort_by: 'sharpe', q: undefined, author: undefined, page: 1, page_size: 12 },
      }));
    }
  });
});
