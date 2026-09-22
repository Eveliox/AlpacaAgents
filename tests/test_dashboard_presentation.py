"""Read-only canvas presentation: honest states and accessible offline evidence."""
from html.parser import HTMLParser
import unittest

from alpaca_agents.agent_desk import project_cycle
from alpaca_agents.agent_desk_view import empty_state, render_desk
from tests.test_agent_desk import cycle, CID


class Elements(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.ids = {}
        self.feed(markup)

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if 'id' in attrs:
            assert attrs['id'] not in self.ids, attrs['id']
            self.ids[attrs['id']] = (tag, attrs)


class CanvasPresentationTests(unittest.TestCase):
    def test_missing_evidence_is_unknown_not_zero_progress(self):
        page = render_desk(empty_state())
        ids = Elements(page).ids
        self.assertIn('open', ids['desk-panel'][1])
        self.assertIn('hidden', ids['desk-progress-meter'][1])
        self.assertIn('Unknown / 5', page)
        self.assertIn('Waiting for evidence', page)
        self.assertIn('Active / waiting states are not captured', page)
        self.assertIn('disabled', ids['desk-motion'][1])
        self.assertIn('No saved cycle. This page does not start one.', page)

    def test_recorded_progress_tasks_and_unbuilt_models_are_distinct(self):
        state = empty_state()
        state['cycles'] = [project_cycle(cycle(), cycle_id=CID)]
        page = render_desk(state)
        ids = Elements(page).ids
        self.assertEqual(ids['desk-progress-meter'][1]['value'], '5')
        self.assertEqual(page.count('class="desk-task"'), 6)
        self.assertEqual(page.count('>Not built</span>'), 2)
        self.assertIn('RECORDED WORKFLOW', page)
        self.assertIn('No communication or delegation events are captured', page)
        self.assertLess(page.index('class="desk-swarm"'), page.index('class="desk-metadata"'))
        self.assertIn('id="desk-evidence"', page)
        self.assertNotIn('<form', page)

    def test_untrusted_agent_names_and_cycle_status_are_escaped(self):
        state = empty_state()
        c = project_cycle(cycle(), cycle_id=CID)
        c['agents'][0]['name'] = '<script>bad()</script>'
        c['status'] = '<img src=x onerror=bad()>'
        state['cycles'] = [c]
        page = render_desk(state)
        self.assertNotIn('<script>', page)
        self.assertNotIn('<img', page)
        self.assertIn('&lt;script&gt;', page)

    def test_halted_cycle_does_not_claim_completed_progress(self):
        state = empty_state()
        raw = cycle()
        raw['stages'] = {'reconcile': {'ok': False, 'reasons': ['MARKET_CLOSED']},
                         'halt': {'reason': 'MARKET_CLOSED'}}
        state['cycles'] = [project_cycle(raw, cycle_id=CID)]
        page = render_desk(state)
        self.assertEqual(Elements(page).ids['desk-progress-meter'][1]['value'], '0')
        self.assertIn('halted', page)
        self.assertIn('MARKET_CLOSED', page)


if __name__ == '__main__':
    unittest.main()
