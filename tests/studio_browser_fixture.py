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
    app = Studio(rt)
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
