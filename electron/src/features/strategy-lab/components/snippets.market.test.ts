import { describe, expect, it } from 'vitest';
import { getStrategyLabSnippets, STRATEGY_LAB_SNIPPETS } from './snippets';

describe('registered Lab examples', () => {
  it.each(['CN', 'US', 'HK'])('preserves the original %s examples', market => {
    expect(getStrategyLabSnippets(market)).toBe(STRATEGY_LAB_SNIPPETS);
  });
  it('uses JP symbols and adjusted daily prices while retaining common risk examples', () => {
    const snippets = getStrategyLabSnippets('JP');
    expect(snippets.length).toBeGreaterThan(10);
    expect(snippets[0].code).toContain('JP72030');
    for (const snippet of snippets) {
      expect(snippet.code).not.toMatch(/sh\d{6}|csi300|SH000300|ctx\.(?:start|end)\s*=/);
      expect(snippet.code).not.toMatch(/ctx\.(?:industry|feature)\(/);
      expect(snippet.code).toContain('默认 history 均使用共用 Qlib 的复权价格');
      expect(snippet.code).toContain('adjust="raw"');
    }
    expect(snippets.some(snippet => snippet.code.includes('ctx.universe = "all"'))).toBe(true);
    expect(snippets.some(snippet => snippet.id === 'long-short-pairs')).toBe(false);
    expect(snippets.some(snippet => snippet.code.includes('ctx.set_stop_loss('))).toBe(true);
    expect(snippets.some(snippet => snippet.code.includes('ctx.set_take_profit('))).toBe(true);
  });
});
