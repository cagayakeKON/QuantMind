import ExcelJS from 'exceljs';
import { beforeEach, describe, expect, it, vi } from 'vitest';
const mocks = vi.hoisted(() => ({save: vi.fn()}));
vi.mock('file-saver', () => ({saveAs: mocks.save}));
import { exportTradeRecordsToExcel } from '../excelExport';

describe('optional currency column in original Excel export', () => {
  beforeEach(() => {vi.clearAllMocks();});
  it.each([undefined, 'JPY'])('preserves original numeric cells and appends a column only for %s', async currency => {
    await exportTradeRecordsToExcel([{时间: '2026/09/29 09:00:00', 方向: '买入', 代码: 'JP72030', 名称: 'Toyota', 数量: 100, 价格: '200.00', 金额: 20000, 状态: '已成交', ...(currency ? {币种: currency} : {})}], 'review.xlsx');
    const blob = mocks.save.mock.lastCall![0] as Blob;
    const buffer = await new Promise<ArrayBuffer>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result as ArrayBuffer);
      reader.onerror = reject;
      reader.readAsArrayBuffer(blob);
    });
    const workbook = new ExcelJS.Workbook();
    await workbook.xlsx.load(buffer as never);
    const sheet = workbook.getWorksheet('交易记录')!;
    expect(sheet.columnCount).toBe(currency ? 9 : 8);
    expect(sheet.getCell('E2').value).toBe(100);
    expect(sheet.getCell('G2').value).toBe(20000);
    if (currency) {expect(sheet.getCell('I1').value).toBe('币种'); expect(sheet.getCell('I2').value).toBe('JPY');}
    else expect(sheet.getCell('I1').value).toBeNull();
  });
});
