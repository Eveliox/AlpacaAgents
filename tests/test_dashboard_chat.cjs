const test = require('node:test');
const assert = require('node:assert/strict');
const {answerFor, redactSecrets} = require('../src/alpaca_agents/assets/dashboard.js');
const topics = Object.fromEntries(['briefing','blockers','risk','positions','orders','notifications','research','scan','controls','next','roles','safety','help'].map(topic => [topic,{text:topic,source:'fixture'}]));
const data = {topics, agents:[{id:'houston',intro:'Houston guide'},{id:'star',intro:'Star guide'},{id:'moon',intro:'Moon guide'},{id:'astra',intro:'Astra guide'}], research:{QQQ:{text:'saved QQQ research',source:'bt-QQQ.json'}}};

test('all suggested questions map to supported topics', () => {
  const cases = {
    'Show my positions':'positions', "Why aren't we trading?":'blockers', 'What happened with orders?':'orders',
    'Compare my backtests':'research', 'Explain QQQ results':'saved QQQ research', 'Why are scans failing?':'scan',
    'Explain my risk limits':'risk', 'Can I trust these results?':'research', 'What does DISABLED mean?':'controls',
    'Give me a briefing':'briefing', 'What should I do next?':'next', 'Show recent notifications':'notifications',
  };
  for (const [question, expected] of Object.entries(cases)) assert.equal(answerFor(question,'astra',data).text,expected,question);
});
test('trading and control requests cannot become actions', () => {
  for(const q of ['buy QQQ now','sell my position','submit the order','enable trend','disable the breaker','arm paper trading','flatten everything','cancel order','change my risk limit','what is the live price of QQQ?','guarantee a profit']) {
    assert.equal(answerFor(q,'houston',data).text,'safety',q);
  }
});
test('personas are distinct but unknown questions are not fabricated', () => {
  for(const a of data.agents) assert.equal(answerFor('Who are you?',a.id,data).text,a.intro);
  assert.equal(answerFor('Explain how to bake a cake','star',data).text,'help');
  assert.equal(answerFor('Which of my agents has feelings?','star',data).text,'help');
});
test('question text is never executed and unrecognized symbols have no invented results', () => {
  assert.equal(answerFor('</script><img src=x onerror=globalThis.injected=true>','moon',data).text,'help');
  assert.equal(globalThis.injected, undefined);
  assert.equal(answerFor('Explain AAPL backtests','star',data).text,'research');
  assert.equal(answerFor('Explain qqq backtests','star',data).source,'bt-QQQ.json');
});
test('possible keys are redacted before they reach chat history', () => {
  assert.equal(redactSecrets('api_key="sampleCredentialValue"'),'[credential redacted]"');
  assert.equal(redactSecrets('abcDEF1234567890abcDEF1234567890'),'[long token redacted]');
  assert.equal(redactSecrets('What are my risk limits?'),'What are my risk limits?');
});
