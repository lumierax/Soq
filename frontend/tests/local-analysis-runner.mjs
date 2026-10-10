// Invoked by local-analysis-parity.py after compiling only the pure TS module.
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';

const require = createRequire(import.meta.url);
const api = require(process.argv[2]);
const operations = JSON.parse(readFileSync(0, 'utf8'), (_key, value) => value === '__NABD_NAN__' ? NaN : value);
const results = operations.map(operation => {
  try {
    let value;
    switch (operation.op) {
      case 'analysis': value = api.analyzeCandles(operation.args); break;
      case 'regime': value = api.regimeFromCandles(operation.candles, operation.interval, operation.nowMs); break;
      case 'closed': value = api.closedCandles(operation.candles, operation.interval, operation.nowMs); break;
      case 'frame': value = api.frameIndicators(operation.candles); break;
      case 'rsi': value = api.rsi(operation.values, operation.period ?? 14); break;
      case 'ema': value = api.ema(operation.values, operation.period); break;
      case 'wilder': value = api.wilder(operation.values, operation.period); break;
      case 'round': value = api.pythonRound(operation.value, operation.digits); break;
      default: throw new Error('Unsupported test operation');
    }
    return { ok: true, value };
  } catch (error) {
    return { ok: false, error: error.message, name: error.name };
  }
});
process.stdout.write(JSON.stringify(results));
