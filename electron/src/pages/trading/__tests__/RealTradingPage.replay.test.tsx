import React from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
const mocks = vi.hoisted(() => ({market: 'JP'}));
vi.mock('../../../store', () => ({useAppSelector: () => mocks.market}));
vi.mock('../../../hooks/useTradeWebSocket', () => ({useTradeWebSocket: () => undefined}));
vi.mock('../../../features/auth/services/authService', () => ({authService: {getAccessToken: () => null}}));
vi.mock('../components/TopBar', () => ({default: () => null}));
vi.mock('../tabs/StrategyConsole/TopologyConsole', () => ({default: () => <div>shared controller</div>}));
vi.mock('../tabs/ManualTaskPage', () => ({default: () => null}));
vi.mock('../tabs/PersonalCenter', () => ({default: () => null}));
vi.mock('../tabs/PositionMonitor', () => ({default: () => null}));
vi.mock('../tabs/TradingHistory', () => ({default: () => null}));
vi.mock('../tabs/SettingsCenter', () => ({default: () => null}));
vi.mock('../tabs/ReplayPage', () => ({default: () => <div>replay workspace</div>}));
vi.mock('../components/LiveTradeConfigWizard', () => ({default: () => null}));
vi.mock('../components/SimulationExecutionInputForm', () => ({default: () => null}));
import RealTradingPage from '../RealTradingPage';

afterEach(cleanup);
describe('registered replay tab visibility', () => {
  it.each(['CN', 'US', 'HK'])('hides content and resets the tab when leaving JP for %s', market => {
    mocks.market = 'JP';
    const view = render(<RealTradingPage />);
    fireEvent.click(screen.getByRole('button', {name: '时光回放'}));
    expect(screen.getByText('replay workspace')).toBeVisible();
    mocks.market = market;
    view.rerender(<RealTradingPage />);
    expect(screen.queryByRole('button', {name: '时光回放'})).not.toBeInTheDocument();
    expect(screen.queryByText('replay workspace')).not.toBeInTheDocument();
    expect(screen.getByText('shared controller')).toBeVisible();
    mocks.market = 'JP';
    view.rerender(<RealTradingPage />);
    expect(screen.queryByText('replay workspace')).not.toBeInTheDocument();
    expect(screen.getByText('shared controller')).toBeVisible();
  });
});
