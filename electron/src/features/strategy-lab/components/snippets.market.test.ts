import { describe, expect, it } from 'vitest';
import { getStrategyLabSnippets, STRATEGY_LAB_SNIPPETS } from './snippets';

describe('registered Lab examples', () => {
  it.each(['CN', 'US', 'HK'])('preserves the original %s examples', market => {
    expect(getStrategyLabSnippets(market)).toBe(STRATEGY_LAB_SNIPPETS);
  });
  it('uses canonical JP symbols, registered all, covered default dates and cash capabilities', () => {
    const snippets = getStrategyLabSnippets('JP');
    expect(snippets.length).toBeGreaterThan(10);
    expect(snippets[0].code).toContain('JP72030');
    for (const snippet of snippets) {
      expect(snippet.code).not.toMatch(/sh\d{6}|csi300|SH000300|ctx\.(?:start|end)\s*=/);
      expect(snippet.code).not.toMatch(/ctx\.(?:set_stop_loss|set_take_profit|set_account_stop_loss|set_max_holding_days|industry|feature)\(/);
      expect(snippet.code).toContain('adjust="qfq"');
    }
    expect(snippets.some(snippet => snippet.code.includes('ctx.universe = "all"'))).toBe(true);
    expect(snippets.some(snippet => snippet.id === 'long-short-pairs')).toBe(false);
  });
});
