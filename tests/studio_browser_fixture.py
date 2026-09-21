"""Isolated browser-test server: synthetic local records, no broker or keys."""
import json
from pathlib import Path
import sys
import time

from alpaca_agents.studio import Studio


def main():
    rt = Path(sys.argv[1])
    rt.mkdir(exist_ok=True)
    (rt / 'bt-QQQ.json').write_text(json.dumps({
        'symbol': 'QQQ', 'level': 'underlying', 'options_pnl_modelled': False,
        'summary': {'trend': {'resolved': 37, 'expectancy_r': 0.25, 'median_r': 0.19,
                              'profit_factor': 1.53, 'max_loss_r': -1.8, 'no_trade': 1}}
    }), encoding='utf-8')
    if '--agent-desk' in sys.argv:
        from decimal import Decimal
        from alpaca_agents.dashboard import build
        from alpaca_agents.executor.orders import OrderJournal
        from alpaca_agents.rules import RiskState
        from tests.test_agent_desk import CID, SECOND, NOW, cycle
        (rt / 'trading-control').write_text('ARMED_PAPER', encoding='utf-8')
        journal = OrderJournal(rt / 'orders.sqlite3', account_id='private-fixture-account', control_file=rt / 'trading-control')
        idea = json.loads((Path(__file__).parents[1] / 'examples/long_call.json').read_text())
        idea.update(playbook='trend_directional', thesis='<img src=x onerror=globalThis.injected=true> api_key=PRIVATEKEY')
        journal.reserve(f'entry-{CID}-IWM-trend_directional', idea,
                        state_provider=lambda: RiskState(NOW.date(), NOW, 0, 0, Decimal(0), Decimal(2000), (), reconciled=True),
                        now=NOW, trading_day=NOW.date())
        latest = cycle(SECOND, 'DISABLED')
        latest['stages'] = {'reconcile': {'ok': False, 'reasons': ['MARKET_CLOSED']}, 'halt': {'reason': 'unreconciled'}}
        (rt / 'cycles.jsonl').write_text(json.dumps(cycle()) + '\n' + json.dumps(latest) + '\n', encoding='utf-8')
        build(rt, rt / 'dashboard.html', now=NOW)
    llm = None
    if '--generative' in sys.argv:
        from alpaca_agents.llm_chat import LLMChat
        from tests.test_llm_chat import FakeTransport, text_reply
        # One rich reply, then deterministic fallbacks. No provider or data calls.
        llm = LLMChat(FakeTransport(text_reply(
            '## Recorded risk\n**$100** maximum entry risk.\n'
            '- Defined-risk options only.\n- Paper account only.\n'
            'https://example.com/source\n<img src=x onerror=globalThis.injected=true>'
        )), model='browser-fixture', daily_cap=100)
    app = Studio(rt, llm=llm)
    original = app.ask

    def ask(agent, text):
        if text == 'slow briefing':
            time.sleep(0.6)
        if text == 'chart test':
            return {'text': 'Isolated chart fixture', 'source': 'Synthetic test', 'charts': [
                '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="60">'
                '<script>parent.injected=true</script><rect width="120" height="60" fill="gold"/>'
                '<text x="2" y="20">test</text></svg>'
            ]}
        return original(agent, text)

    app.ask = ask
    app.start()
    print(app.url, flush=True)  # URL only; token never appears in test output.
    try:
        while sys.stdin.readline():
            pass
    finally:
        app.close()


if __name__ == '__main__':
    main()
