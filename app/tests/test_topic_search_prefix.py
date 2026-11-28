#!/usr/bin/env python3
"""Topic search regression list for #333: a short word that opens a guide because it starts a longer word.

  python3 -B -m unittest app.tests.test_topic_search_prefix [-v]      # the checks
  python3 -B app/tests/test_topic_search_prefix.py                    # the report

A search word matches the start of any word in a guide's title, keywords, labels and hints. That is what lets
"cook" find "cooking" and "warm" find "warming". It is also why "pain" finds "paint" and "fall" finds "fallout".
This module keeps both lists side by side, so a change to the matching or the ranking can be measured:

  FALSE_MATCHES   searches where the first result comes from an unrelated longer word. Recorded, not asserted
                  as failures: they are wrong on main today, and a test that fails on main would make every
                  other PR red. The list may only shrink: once a row is taken out of KNOWN, the check fails if
                  that search goes back to its wrong result. A guide PR that fixes a row does not have to touch
                  this file; the report then shows the row as "fixed", and whoever merges it can drop it from
                  KNOWN to pin the fix.
                  Each row also says which first results would be right, where that is clear from the guides.
                  A row is "fixed" only when its first result is one of those. Any other new first result,
                  and every new result of a row with no clear answer, is "changed": it left one wrong result
                  and nobody has judged the new one. "changed" never counts as fixed.
  KEEP            searches that depend on a short word starting a longer one, and must keep their first result.

This is a test only: no search code is touched. The searches run through `web/words.js` `rankTopics`, the
function the app calls, so no library or data folder is needed. Needs Node on PATH, as test_topic_search does;
never packaged.
"""
import json, re, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
NODE = shutil.which('node')
WORDS = ROOT / 'web' / 'words.js'
TOPICS = [json.loads(p.read_text(encoding='utf-8')) for p in sorted((ROOT / 'content' / 'topics').glob('*.json'))]
QSTOP = re.search(r"const QSTOP = new Set\('([^']*)'\.split", (ROOT / 'web' / 'app.js').read_text(encoding='utf-8')).group(1).split(' ')

# upstream/main these were last re-measured on. Re-measure on a new main and record the new one.
BASELINE = '40bac14'

#   (search, was, right, why), where:
#     was    the wrong first result this row was recorded with -- the unrelated longer word winning. It is what the
#            row guards against, not what the search gives today: a row listed in FIXED no longer returns it, and
#            that is the point of the row. A search that goes back to its "was" is the regression this file catches.
#     right  the first results that would be right, as choices that name the subject in their own label or hint;
#            () where the guides give no clear answer, so any new result is reported as "changed".
#     why    the short word and the longer word it starts.
FALSE_MATCHES = [
    ('stomach pain', ['grow', 'soil-unknown'], (['health', 'start'], ['hurt', 'diarrhea']), 'pain / paint ("lead paint")'),
    ('left arm pain', ['grow', 'soil-unknown'], (['health', 'start'], ['hurt', 'sudden']), 'pain / paint'),
    ('fall from ladder', ['disaster', 'nuclear-explosion'], (['hurt', 'bone'], ['hurt', 'head']), 'fall / fallout'),
    ('elderly fall', ['disaster', 'nuclear-explosion'], (['hurt', 'bone'], ['hurt', 'head']), 'fall / fallout'),
    ('want to die', ['food', 'special'], (['health', 'mind'],), 'die / diets'),
    ('the loo wont flush', ['hurt', 'bone'], (['sanitation', 'noflush'],), 'loo / looks ("looks bent")'),
    ('rat bite', ['hurt', 'snakebite'], (['hurt', 'animal-bite'],), 'rat / rattlesnake'),
    ('rat poison', ['hurt', 'snakebite'], (['hurt', 'poison'],), 'rat / rattlesnake'),
    ('ear infection', ['disaster', 'earthquake-now'], (), 'ear / earthquake'),
    ('head lice', ['comms', 'ham'], (), 'lice / license'),
    ('bat in house', ['power', 'batteries-solar'], (), 'bat / batteries'),
    ('car stuck in snow', ['hurt', 'breathing'], (), 'car / cardiac, with stuck from "food stuck in throat"'),
    ('war', ['shelter', 'cold'], (), 'war / warm'),
    ('what to pack', ['disaster', 'explosion'], (['disaster', 'pack'],), 'pack / package ("suspicious package")'),
    ('out of formula', ['food', 'outage'], (['food', 'special'],), 'out / outage'),
    ('ran out of insulin', ['food', 'outage'], (['health', 'chronic'],), 'out / outage'),
    ('high fever', ['disaster', 'highrise-fire'], (['health', 'start'],), 'high / high-rise'),
]
# The rows whose false match is gone. Adding a row here pins its fix: it now lands on a result the row lists as
# right, and putting it back on its "was" fails test_false_matches_only_shrink. Each entry says what fixed it.
# Eleven rows so far.
FIXED = {
    'stomach pain', 'left arm pain',    # #326, "pain" is a keyword of the health and hurt guides
    'fall from ladder', 'elderly fall',  # #327, "fall" and "fell" are keywords of the first aid guide
    'the loo wont flush',               # the plumbing hint names a leaking toilet
    'rat bite',                         # #455, phrases on the animal bite choice
    'rat poison',                       # #331, the poison hint names rat poison
    'out of formula', 'ran out of insulin',   # #455, phrases on the feeding and chronic illness choices
    'high fever',                       # #330, the health start hint names a high fever
    'what to pack',                     # #432, the evacuation guide has a packing choice of its own
}
# What is still wrong on BASELINE: every row above that FIXED has not claimed.
KNOWN = {q for q, _, _, _ in FALSE_MATCHES} - FIXED

#   (search, the first result that must hold, the short word and the longer word it relies on)
KEEP = [
    ('stay warm', ['shelter', 'cold'], 'warm / warming'),
    ('cook without power', ['food', 'cook'], 'cook / cooking'),
    ('load shedding', ['power', None], 'load / loadshedding'),
    ('car battery', ['power', 'batteries-solar'], 'car and battery, both whole words on the choice'),
    ('how to dry meat', ['preserve', 'method'], 'dry / drying'),
    ('burn', ['hurt', 'burn'], 'burn / burns, burned, burnt'),
    ('flood', ['disaster', 'flood'], 'flood / flooding, floodwater'),
    ('freez', ['food', 'outage'], 'a word half typed: freez / freezer'),
    ('gen', ['power', 'generator'], 'a word half typed: gen / generator'),
]

CASES = {'stop': QSTOP, 'topics': TOPICS, 'questions': [q for q, _, _, _ in FALSE_MATCHES] + [q for q, _, _ in KEEP]}


def run():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / 'cases.json'
        p.write_text(json.dumps(CASES, ensure_ascii=False), encoding='utf-8')
        r = subprocess.run([NODE, str(HERE / 'topic_search_emergency.mjs'), str(WORDS), str(p)],
                           capture_output=True, text=True, encoding='utf-8', timeout=60)
    if r.returncode:
        raise AssertionError(r.stderr[-1500:])
    return json.loads(r.stdout)


def first(result):
    """The guide and choice that lead, as [guide, choice], or None when no guide matched."""
    top = result[0] if result else None
    return [top[0], top[1]] if top else None


def state(got, was, right):
    """A recorded false match now: 'still' on its wrong result, 'fixed' on a result listed as right, else 'changed'."""
    return 'still' if got == was else 'fixed' if got in [list(r) for r in right] else 'changed'


@unittest.skipUnless(NODE, 'needs node on PATH')
class TopicSearchPrefix(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.got = dict(zip(CASES['questions'], (first(r) for r in run())))

    def test_false_matches_only_shrink(self):
        still = {q for q, was, _, _ in FALSE_MATCHES if self.got[q] == was}
        self.assertLessEqual(still, KNOWN, 'a false match that was fixed is back')

    def test_a_row_out_of_known_is_fixed_not_changed(self):
        # Taking a row out of KNOWN pins a fix, so the row needs a right result and must be on it.
        for q, was, right, why in FALSE_MATCHES:
            if q not in KNOWN:
                with self.subTest(q=q, why=why):
                    self.assertEqual(state(self.got[q], was, right), 'fixed')

    def test_changed_is_never_fixed(self):
        # One wrong result swapped for another, or for no result, is "changed"; so is any new result of a row
        # with no right result listed. Only a listed result is "fixed".
        was, right = ['grow', 'soil-unknown'], (['health', 'start'], ['hurt', 'diarrhea'])
        self.assertEqual(state(was, was, right), 'still')
        self.assertEqual(state(['hurt', 'diarrhea'], was, right), 'fixed')
        self.assertEqual(state(['repair', 'build'], was, right), 'changed')
        self.assertEqual(state(['health', None], was, right), 'changed')
        self.assertEqual(state(None, was, right), 'changed')
        self.assertEqual(state(['health', 'start'], was, ()), 'changed')
        self.assertEqual(state(was, was, ()), 'still')

    def test_right_results_are_real_choices_and_not_the_wrong_one(self):
        choices = {(t['id'], c['id']) for t in TOPICS for c in t.get('choices', [])}
        for q, was, right, _ in FALSE_MATCHES:
            with self.subTest(q=q):
                self.assertNotIn(was, [list(r) for r in right])
                for r in right:
                    self.assertIn(tuple(r), choices)

    def test_useful_prefix_matches_keep_working(self):
        for q, want, why in KEEP:
            with self.subTest(q=q, why=why):
                self.assertEqual(self.got[q], want)


def report():
    got = dict(zip(CASES['questions'], (first(r) for r in run())))
    print(f'baseline: {BASELINE}')
    print('\nFalse matches (first result comes from an unrelated longer word)')
    count = {'still': 0, 'fixed': 0, 'changed': 0}
    for q, was, right, why in FALSE_MATCHES:
        st = state(got[q], was, right)
        count[st] += 1
        note = ', can leave KNOWN' if st == 'fixed' and q in KNOWN else ', not judged' if st == 'changed' else ''
        print(f"  {st}{note}  {q!r}  {got[q]}  ({why})")
    print('\nUseful prefix matches (must keep working)')
    for q, want, why in KEEP:
        print(f"  {'ok   ' if got[q] == want else 'MOVED'}  {q!r}  {got[q]}  ({why})")
    print(f"\nfalse matches of {len(FALSE_MATCHES)}: {count['still']} still, {count['fixed']} fixed, "
          f"{count['changed']} changed (not judged)")
    print(f"useful matches kept: {sum(got[q] == want for q, want, _ in KEEP)} of {len(KEEP)}")


if __name__ == '__main__':
    if NODE and len(sys.argv) == 1:
        report()
    else:
        unittest.main()
