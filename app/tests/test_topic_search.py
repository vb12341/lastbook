#!/usr/bin/env python3
"""Topic search beyond a-z: web/words.js run in Node, with Intl.Segmenter, without it, and with one that throws.

  python3 -B -m unittest app.tests.test_topic_search [-v]

Accented words stay whole and fold (niño finds nino), short words in Chinese, Hindi, Hebrew and Arabic count,
typed symbols never reach a RegExp, Turkish İ folds to i, and every English query in the list ranks exactly as it
did before. Words used outside the US (torch, rubbish, 999, 108, 1122, power cut, loadshedding, hydro, forest fire)
find the guide their US word finds. A search that finds nothing is tried again by word form (tourniquets,
fainting, choked). Needs Node on PATH; skipped without it. Never packaged."""
import json, re, shutil, subprocess, tempfile, unicodedata, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
NODE = shutil.which('node')
WORDS = ROOT / 'web' / 'words.js'

# The stop words app.js passes in, read from app.js so the test follows it.
QSTOP = re.search(r"const QSTOP = new Set\('([^']*)'\.split", (ROOT / 'web' / 'app.js').read_text(encoding='utf-8')).group(1).split(' ')
TOPICS = [json.loads(p.read_text(encoding='utf-8')) for p in sorted((ROOT / 'content' / 'topics').glob('*.json'))]

# Everyday English questions. Each must give the same topics and choices as the old a-z matching,
# including words joined by punctuation, which the old split took apart.
ENGLISH = [
    'how do I purify water', 'boil water', 'is the tap water safe', 'bleach for drinking water', 'find water',
    'deep cut bleeding', 'broken leg', 'burn on my hand', 'cpr', 'choking', 'fever', 'dehydration', 'snake bite',
    'hypothermia', 'the power is out', 'generator', 'solar panel', 'car battery', 'earthquake', 'flood',
    'wildfire smoke', 'store food', 'cook without power', 'stay warm', 'shelter', 'toilet', 'radio',
    'grow vegetables', 'save seeds', 'first aid kit', "water's gone brown", "don't have power", "can't breathe",
    'how to start a fire', 'baby', 'medicine', 'keep food cold', 'clean water storage container',
    'water.boil', 'first_aid kit', 'e.coli', 'boil:water', 'https://example.org/water',
]

# Everyday words a guide covers, and the guide and choice search must show first, with and without Intl.Segmenter.
# Most found nothing or the wrong guide until the word was added to the hint of the choice that covers it. A word only
# in a topic's keywords is not enough: another word then picks the choice ("poisoned water" went to burns, for "hot water").
EVERYDAY = {
    'burned my hand': ['hurt', 'burn'], 'burnt': ['hurt', 'burn'],
    'fractured arm': ['hurt', 'bone'], 'dislocated shoulder': ['hurt', 'bone'], 'poisoned': ['hurt', 'poison'],
    'heatstroke': ['hurt', 'heatcold'], 'throwing up': ['hurt', 'diarrhea'], 'dehydrated': ['hurt', 'diarrhea'],
    'diarrhoea': ['hurt', 'diarrhea'], 'suicidal': ['health', 'mind'],
    'bushfire smoke': ['disaster', 'smoke'],
    'poisoned water': ['water', 'chemical'], 'drank poisoned water': ['water', 'chemical'],
    'dehydrated water': ['hurt', 'diarrhea'], 'throwing up water': ['hurt', 'diarrhea'], 'diarrhoea water': ['hurt', 'diarrhea'],
    'fractured neck': ['hurt', 'head'], 'fractured back': ['hurt', 'head'], 'dislocated neck': ['hurt', 'head'],
    'fractured skull': ['hurt', 'head'], 'fractured spine': ['hurt', 'head'], 'dislocated spine': ['hurt', 'head'],
    'spinal fracture': ['hurt', 'head'], 'fractured vertebra': ['hurt', 'head'], 'fractured vertebrae': ['hurt', 'head'],
    'vertebral fracture': ['hurt', 'head'], 'fractured vertebral column': ['hurt', 'head'],
    'I drank from the stream and have diarrhea': ['hurt', 'diarrhea'], 'I drank and feel dehydrated': ['hurt', 'diarrhea'],
    'carbon monoxide': ['shelter', 'co'], 'carbon monoxide poisoning': ['shelter', 'co'],
}

# Injuries in a forest: since "forest fires" joined the wildfire hint, "forest" also picks wildfire, so these only have
# to show Broken bone somewhere in the results, not first.
LISTED = {
    'I fractured my arm in the forest': ['hurt', 'bone'], 'I dislocated my shoulder in the forest': ['hurt', 'bone'],
    'I had a fall in the forest': ['hurt', 'bone'],
}

# Query -> words, the same with or without Intl.Segmenter (every script here uses spaces).
WORDS_BOTH = {
    '¿Cómo purifico el agua para el niño?': ['como', 'purifico', 'agua', 'para', 'nino'],   # accents whole, then folded
    'niño': ['nino'],                                     # precomposed ñ
    'niño': ['nino'],                                    # n + combining tilde (NFD)
    'crème brûlée': ['creme', 'brulee'],        # every accent goes, not just the first
    "How do I purify water's taste?": ['purify', 'water', 'taste'],   # water's -> water, as before
    'don’t have water': ['water'],                         # curly apostrophe, stop words
    'ｗａｔｅｒ': ['water'],                # full-width Latin
    'water.boil first_aid e.coli boil:water': ['water', 'boil', 'first', 'aid', 'coli', 'boil', 'water'],
    'water­proof': ['water', 'proof'],                     # soft hyphen separates, as in FTS5
    'می‌خواهم': ['می', 'خواهم'],   # ZWNJ separates
    'आग אש دم': ['आग', 'אש', 'دم'],   # two-letter words without case
    'पानी उबालें': ['पानी', 'उबालें'],   # marks stay in words
    'ab 42 112 go': ['112'],                                    # short Latin words and numbers dropped, as before
    '\U00010428\U0001042f': [],                                # two letters outside the BMP: still two, still too short
    'c++ (water': ['water'],
    '[a': [], '(': [], '\\': [], '*?+{': [], '': [],            # symbols: no words, no exception
    'İstanbul': ['istanbul'],
    'éclair': ['eclair'],
}

FOLD = {
    'İstanbul': 'istanbul', 'ISTANBUL': 'istanbul', 'ılık': 'ılık',   # dotless ı stays
    'Straße': 'straße', 'ΣΟΦΟΣ': 'σοφοσ',   # ß stays, ς -> σ, as FTS5
    'ｗａｔｅｒ': 'water', 'niño': 'nino', 'niño': 'nino', 'Jalapeño': 'jalapeno',
    'καφές': 'καφέσ',   # Greek accents kept, as in FTS5
    'crème brûlée': 'creme brulee',
}

MATCH = [  # text, word, expected
    ['chocolate éclair', 'eclair', True], ['Chocolate Éclair', 'eclair', True],
    ['煮沸 水', '水', True], ['怎么净化水', '净化', True],
    ['drink water', 'rink', False], ['drink water', 'drin', True], ['Rainwater', 'water', False],
    ['Straße', 'strasse', False], ['ΣΟΦΟΣ', 'σοφοσ', True],
    ['İstanbul', 'istanbul', True], ['72-hour kit', 'hour', True], ['Water: purify, boil', 'purif', True],
]

# Words used outside the US, each with the US word the guides already had. A search for the first must give
# the same guides and choices, in the same order, as a search for the second.
SAME_AS = {
    'torch': 'flashlight', 'rubbish': 'garbage', 'rubbish pickup': 'trash pickup', 'tinned food': 'canned food',
    '999': '911', '112': '911', '111': '911', 'adrenaline': 'epinephrine',
    '108': '911', 'geyser': 'water heater',
    '1122': '911', '115': '911',   # Rescue 1122 and Edhi in Pakistan
}
# These sit in a guide's keywords only, so they open the same guide first but pick no choice of their own.
# In a hint, "bushfire" would also answer "bus" and "barbecue" would answer "bar" (a search word matches the
# start of a word), and "heatwave" or "sanitiser" beside the US spelling would read oddly.
SAME_GUIDE = {
    'bushfire': 'wildfire', 'bushfire smoke': 'wildfire smoke', 'heatwave': 'heat wave', 'barbecue': 'grill',
    'bbq': 'grill', 'sanitiser': 'sanitizer', 'hand sanitiser': 'hand sanitizer',
    'heatstroke': 'heat stroke',
    'snowstorm': 'winter storm', 'snow squall': 'winter storm', 'whiteout': 'winter storm',
    'polar vortex': 'winter storm', 'wind chill': 'winter storm', 'arctic outflow': 'winter storm',
    'warming centre': 'winter storm', 'aqhi': 'air quality', 'AQHI': 'air quality',
    'cooling centre': 'cooling center', 'washroom': 'sanitation',
    # Pakistan's scheduled power cuts. Written as one word in the keywords: "load shedding" as two would let
    # "shed" open the power guide, and "load" in a hint would take "generator load" away from the generator choice.
    'loadshedding': 'power cut', 'load shedding': 'power cut', 'load-shedding': 'power cut',
}
# Words used in South Asia and Canada with no single US twin among the guides: [guide, choice] that comes first.
FIRST = {
    'power cut': ['power', 'first-hours'], 'cyclone': ['disaster', 'flood'], 'monsoon': ['disaster', 'flood'],
    'borewell': ['water', 'outside'], 'tubewell': ['water', 'outside'], 'loose motions': ['hurt', 'diarrhea'],
    'hydro': ['power', 'first-hours'], 'hydro is out': ['power', 'first-hours'],
    'hydro outage': ['power', 'first-hours'], 'ice storm': ['shelter', 'cold'], 'freezing rain': ['shelter', 'cold'],
    'extreme cold': ['shelter', 'cold'], 'cold snap': ['shelter', 'cold'], 'blizzard': ['shelter', 'cold'],
    'furnace': ['shelter', 'cold'], 'furnace is out': ['shelter', 'cold'], 'frostnip': ['hurt', 'heatcold'],
    'forest fire': ['disaster', 'wildfire'], 'forest fires': ['disaster', 'wildfire'],
    'forest fire smoke': ['disaster', 'smoke'], 'Hydro': ['power', 'first-hours'], 'Frostnip': ['hurt', 'heatcold'],
    'cold': ['shelter', 'cold'], 'its cold': ['shelter', 'cold'],
    'house fire': ['disaster', 'home-fire'], 'my house is on fire': ['disaster', 'home-fire'],
    'smog': ['shelter', 'smoke'],   # winter smog in Lahore and Delhi; the choice cites a page on particles and ozone
}
# Words that a reading already cited under a choice is about, but that no title, keyword, label or hint held, so a search
# for them found nothing: [guide, choice] that comes first.
ALREADY_COVERED = {
    'pets': ['food', 'special'], 'propane': ['power', 'gas'], 'propane tank': ['power', 'gas'], 'dental': ['health', None],
    'respirator': ['disaster', 'smoke'], 'N95 respirator': ['disaster', 'smoke'], 'greenhouse': ['repair', 'build'],
    'toothpaste': ['health', None], 'bandage': ['hurt', None], 'gauze': ['hurt', None], 'goggles': ['repair', None],
    'antenna': ['comms', None], 'documents': ['disaster', None], 'important documents': ['disaster', None],
    'bruise': ['hurt', 'bone'], 'sling': ['hurt', 'bone'], 'stretcher': ['hurt', 'nohelp'], 'stinger': ['hurt', 'sting'],
    'graze': ['hurt', 'closing'], 'spoilage': ['preserve', 'jarcheck'], 'distillation': ['water', 'chemical'],
    'honing': ['repair', 'dull'],
    'fermentation': ['preserve', None], 'kombucha': ['preserve', None], 'radiological': ['disaster', None],
}
# An emergency that happens to mention a new word still opens the urgent choice first, not the routine one the word was added to.
URGENT_FIRST = {
    'child unconscious after pet food': ['hurt', 'breathing'], 'child swallowed pet food': ['hurt', 'poison'],
    'child not breathing respirator': ['hurt', 'breathing'], 'child swallowed toothpaste': ['hurt', 'poison'],
    'bleeding through the bandage': ['hurt', 'bleeding'], 'bleeding wont stop bandage soaked': ['hurt', 'bleeding'],
    'unconscious bleeding bandage': ['hurt', 'breathing'], 'bandage a burn': ['hurt', 'burn'], 'duct tape wound': ['hurt', 'bleeding'],
    'bruise on head after fall': ['hurt', 'head'], 'head injury bruise': ['hurt', 'head'], 'bruised and bleeding': ['hurt', 'bleeding'],
    'graze bleeding heavily': ['hurt', 'bleeding'], 'bleeding stinger': ['hurt', 'bleeding'], 'stinger throat swelling': ['hurt', 'breathing'],
    'stretcher neck injury': ['hurt', 'head'], 'carry on stretcher not breathing': ['hurt', 'breathing'],
    'pain in left arm and chest': ['hurt', 'sudden'],
}
# Searches the new words must not disturb: [guide, choice] that comes first.
UNDISTURBED = {
    'bus': ['disaster', 'no-car'], 'no bus': ['disaster', 'no-car'], 'bar soap': ['sanitation', 'hands'],
    'heat wave': ['shelter', 'heat'], 'grill': ['food', 'cook'], 'canned food': ['food', 'cook'],
    # Everyday phrases that opened the wrong guide or none: "no electricity" opened Burn or scald, "high fever" the
    # high-rise fire choice, and "bathroom" and "no cell service" found nothing. The rest are guards. The word is on
    # the long outage choice only: on "The power just went out" as well, "electric shock from a downed power line"
    # left the damaged home choice (test_topic_search_emergency).
    'no electricity': ['power', 'long-outage'], 'electricity': ['power', 'long-outage'],
    'no electricity for days': ['power', 'long-outage'], 'electricity burn': ['hurt', 'burn'], 'electric shock': ['hurt', 'burn'],
    'high fever': ['health', 'start'], 'baby has a high fever': ['health', 'start'], 'high blood pressure': ['health', 'chronic'],
    'bathroom': ['sanitation', None], 'where to go to the bathroom': ['sanitation', None], 'bat bite': ['hurt', 'animal-bite'],
    'no cell service': ['comms', 'phones-down'], 'radio reception': ['comms', 'alerts-radio'], 'service dog': ['hurt', 'animal-bite'],
    'cut': ['hurt', 'bleeding'], 'well': ['land', 'water-source'], 'storm': ['disaster', 'flood'],
    # Words used in Britain, Australia, India and elsewhere for things the guides already cover. The last five are guards.
    'petrol': ['power', 'generator'], 'kerosene': ['shelter', 'co'], 'paraffin': ['shelter', 'co'], 'aircon': ['shelter', 'heat'],
    'powerbank': ['power', 'batteries-solar'], 'cooker': ['food', 'cook'], 'hob': ['food', 'cook'], 'mobile': ['comms', None],
    'the loo wont flush': ['sanitation', 'noflush'], 'mobile phone': ['comms', 'phones-down'], 'smell gas': ['power', 'gas'],
    'looks bent': ['hurt', 'bone'], 'collected rainwater': ['land', None], 'kerosene heater': ['shelter', 'co'],
    'ice': ['water', 'inhome'], 'micro-hydro': ['energy', None], 'wind': ['disaster', 'hurricane'],
    # Other forms of first aid words that found nothing ("threw up", "electrocuted", "stung"), and "rat poison",
    # which opened Snakebite because "rat" starts "rattlesnake". The last four are guards.
    'threw up': ['hurt', 'diarrhea'], 'puking': ['hurt', 'diarrhea'], 'electrocuted': ['hurt', 'burn'],
    'rat poison': ['hurt', 'poison'], 'ate rat poison': ['hurt', 'poison'], 'stung': ['hurt', None], 'sunstroke': ['hurt', None],
    'frostbitten': ['hurt', None], 'overheated': ['hurt', None], 'electrocuted not breathing': ['hurt', 'breathing'],
    "stung can't breathe": ['hurt', 'breathing'], 'bad food': ['food', 'outage'], 'throwing up': ['hurt', 'diarrhea'],
    'winter storm': ['shelter', 'cold'], 'furnace smells like gas': ['power', 'gas'], 'gas furnace': ['power', 'gas'],
    # "fall" starts "fallout": a fall opens first aid, not the nuclear choice, and fallout still opens the nuclear choice.
    'fall': ['hurt', 'bone'], 'fall from ladder': ['hurt', 'bone'], 'elderly fall': ['hurt', 'bone'], 'fell': ['hurt', 'head'],
    'fell off ladder': ['hurt', 'head'], 'slipped and fell': ['hurt', 'head'], 'fall hit head': ['hurt', 'head'],
    'fall not breathing': ['hurt', 'breathing'], 'fell unconscious': ['hurt', 'breathing'],
    'fallout': ['disaster', 'nuclear-explosion'], 'nuclear fallout': ['disaster', 'nuclear-explosion'],
    'fallout shelter': ['disaster', 'nuclear-explosion'],
    'lost in the forest': ['comms', 'signal'],
    'baby formula': ['food', 'special'], 'food allergies': ['food', 'outage'], 'spoiled food': ['food', 'outage'],
    # "pack" starts "package", so packing searches opened the suspicious package choice. The packing choice cites the
    # two readings on what to take from the guide's own preparation list. Its words are phrases, counted only when
    # the rest of the search is everyday words, so a search about something else keeps its guide. The rest are guards.
    'what to pack': ['disaster', 'pack'], 'pack a bag': ['disaster', 'pack'], 'packing': ['disaster', 'pack'],
    'go bag': ['disaster', 'pack'], 'what to pack to evacuate': ['disaster', 'pack'],
    'flood packing': ['disaster', 'flood'], 'ice pack': ['disaster', 'explosion'], 'pack wound': ['hurt', 'bleeding'],
    'what to pack for a hurricane': ['disaster', 'hurricane'], 'sleeping bag': ['disaster', None],
    'suspicious package': ['disaster', 'explosion'], 'unattended package': ['disaster', 'explosion'],
    'take shelter': ['disaster', 'shelter-in-place'],
    'pet food': ['food', 'outage'],
    'dentist': ['health', None], 'toothache': ['health', 'teeth'],
    'cold frame': ['repair', 'build'], 'freezing': ['shelter', 'cold'],
    # "toilet" tied between the repair guide and the toilets guide, and repair is listed first. An emergency search
    # now opens the toilets guide; a repair search keeps the plumbing choice.
    'toilet': ['sanitation', 'noflush'], 'no toilet': ['sanitation', 'noflush'], 'toilet clogged': ['sanitation', 'noflush'],
    'toilet overflowing': ['sanitation', 'noflush'], 'toilet keeps running': ['repair', 'plumbing'],
    'fix toilet': ['repair', 'plumbing'], 'toilet leaking': ['repair', 'plumbing'], 'dripping tap': ['repair', 'plumbing'],
    'fire alarm': ['disaster', 'home-fire'], 'smoke alarm': ['disaster', 'home-fire'], 'smoke': ['disaster', 'smoke'],
    'fires after an earthquake': ['disaster', 'earthquake-after'],
    # Words from readings the throw-out choice already cites (refreezing thawed food, dented cans). The last four are guards.
    'refreeze': ['food', 'throwout'], 'can i refreeze': ['food', 'throwout'], 'defrosted chicken': ['food', 'throwout'],
    'dented can': ['food', 'throwout'], 'thaw frozen pipes': ['water', 'inhome'], 'freezer thawed': ['food', 'outage'],
    'frostbite thawed': ['hurt', 'heatcold'], 'swollen can': ['food', 'throwout'],
    'shed': ['repair', None], 'generator load': ['power', 'generator'], 'air quality': ['shelter', 'smoke'],
}
# A search that names a sign of a life-threatening emergency shows that choice, even if another choice in the guide
# matches more words. The rest don't say the person isn't breathing or is unconscious, and keep their choice.
URGENT = {
    'burn and not breathing': ['hurt', 'breathing'], 'broken leg and not breathing': ['hurt', 'breathing'],
    'cut and not breathing': ['hurt', 'breathing'], 'fractured neck and not breathing': ['hurt', 'breathing'],
    'dislocated neck unconscious': ['hurt', 'breathing'], 'my baby stopped breathing': ['hurt', 'breathing'],
    "he isn't breathing": ['hurt', 'breathing'], 'choking': ['hurt', 'breathing'],
    'allergic reaction trouble breathing': ['hurt', 'allergy'], "can't breathe after a bee sting": ['hurt', 'allergy'],
    'gas burn': ['hurt', 'burn'], 'broken leg stuck under rubble': ['hurt', 'bone'],
    'wound infection not responding to antibiotics': ['hurt', 'bleeding'],
    'allergic reaction not responding to epinephrine': ['hurt', 'allergy'],
    'allergic reaction choking': ['hurt', 'allergy'], 'burn and choking on smoke': ['hurt', 'burn'],
}
# A breathing emergency after a sting must not open routine sting care.
NOT_STING = ["bee sting can't breathe", 'bee sting cannot breathe', 'bee sting unconscious', 'wasp sting unconscious']
# A search starts a word, so "pain" also finds "paint" in the garden soil hint ("lead paint"). Pain opens a medical guide
# first, and paint still opens the soil choice.
PAIN_NOT_PAINT = {
    'pain': ['health', 'start'], 'stomach pain': ['health', 'start'], 'back pain': ['health', 'start'],
    'left arm pain': ['health', 'start'], 'jaw pain': ['health', 'start'], 'severe pain': ['health', 'start'],
    'child in pain': ['health', 'start'], 'chest pain': ['hurt', 'sudden'], 'burn pain': ['hurt', 'burn'],
    'paint': ['grow', 'soil-unknown'], 'lead paint': ['grow', 'soil-unknown'], 'peeling paint': ['grow', 'soil-unknown'],
    'swallowed paint': ['hurt', 'poison'],
}
TOPS = sorted({*SAME_AS, *SAME_AS.values(), *SAME_GUIDE, *SAME_GUIDE.values(), *FIRST, *ALREADY_COVERED, *URGENT_FIRST, *UNDISTURBED, *URGENT, *EVERYDAY, *LISTED, *NOT_STING, *PAIN_NOT_PAINT})

# Words used in Thailand and Denmark. Emergency numbers find what 911 finds; haze, PM2.5 and cloudburst sit in a
# guide's keywords only and open the guide their US word opens. Thailand's fire number, 199, is left out: a fire
# number must not open first aid (as the UAE's 997 in #98).
TH_DK_SAME_AS = {'1669': '911', '191': '911', '1813': '911'}
NOT_FIRST_AID = ['199']
TH_DK_SAME_GUIDE = {'haze': 'air quality', 'PM2.5': 'air quality', 'pm2.5': 'air quality', 'cloudburst': 'flood'}
# Searches those words must not disturb: [guide, choice] that comes first.
TH_DK_UNDISTURBED = {'cloud': ['disaster', 'tornado'], 'funnel cloud': ['disaster', 'tornado'],
                     'burn': ['hurt', 'burn'], 'flood': ['disaster', 'flood'], 'mask': ['shelter', None]}
TOPS = sorted({*TOPS, *TH_DK_SAME_AS, *TH_DK_SAME_GUIDE, *TH_DK_SAME_GUIDE.values(), *TH_DK_UNDISTURBED,
              'burning', 'season', *NOT_FIRST_AID})

# A question about her ranks exactly as the same question about him. A guide word only has to start with a search
# word, so "she" found "shelter" and put Shelter in place first; "her" and "his" found "here" and "hissing".
SAME_FOR_HIM = {
    'she is bleeding': 'he is bleeding', "she can't breathe": "he can't breathe", 'is she breathing': 'is he breathing',
    'she is choking': 'he is choking', 'she fell and hit her head': 'he fell and hit his head',
    'she burned her hand': 'he burned his hand', 'she has a fever': 'he has a fever',
    'her arm is bleeding': 'his arm is bleeding',
}
# Typing "shelter" still finds it from "shel": [guide, choice] that comes first.
STILL_SHELTER = {'shel': ['disaster', 'shelter-in-place'], 'shelter': ['disaster', 'shelter-in-place'],
                 'shelter in place': ['disaster', 'shelter-in-place']}
TOPS = sorted({*TOPS, *SAME_FOR_HIM, *SAME_FOR_HIM.values(), *STILL_SHELTER})

# A made-up topic in Spanish and Chinese: found by Spanish words and by a single Han character.
OWN = {'id': 'own', 'title': 'Agua potable', 'short': 'Hervir el agua', 'keywords': ['水'], 'choices': []}
RANK_OWN = [['agua', [OWN]], ['水', [OWN]], ['hervir', [OWN]]]

# Emergencies where the first choice decides what someone reads first. A cardiac arrest needs CPR, not the
# long-term care guide that "heart" also matches: [guide, choice] that comes first.
EMERGENCY_FIRST = {
    'cardiac arrest': ['hurt', 'breathing'], 'heart stopped': ['hurt', 'breathing'], 'AED': ['hurt', 'breathing'],
    'aed': ['hurt', 'breathing'], 'defibrillator': ['hurt', 'breathing'], 'heart attack': ['hurt', 'sudden'],
    'having a heart attack': ['hurt', 'sudden'], 'signs of heart attack': ['hurt', 'sudden'],
    # A stroke needs the stroke signs, not heat illness, which also says "stroke" ("heat stroke") and is listed first.
    'stroke': ['hurt', 'sudden'], 'a stroke': ['hurt', 'sudden'], 'having a stroke': ['hurt', 'sudden'],
    'my dad is having a stroke': ['hurt', 'sudden'], 'stroke symptoms': ['hurt', 'sudden'], 'mini stroke': ['hurt', 'sudden'],
}
# Searches those words must not disturb: [guide, choice] that comes first.
EMERGENCY_UNDISTURBED = {
    'heart': ['health', 'chronic'], 'heart disease': ['health', 'chronic'], 'blood pressure': ['health', 'chronic'],
    'chest pain': ['hurt', 'sudden'], 'collapsed': ['hurt', 'breathing'], 'not breathing': ['hurt', 'breathing'],
    'drowning': ['hurt', 'breathing'], 'cpr': ['hurt', None], 'stopped': ['disaster', 'earthquake-after'],
    'heat stroke': ['hurt', 'heatcold'], 'sun stroke': ['hurt', 'heatcold'], 'heat stroke symptoms': ['hurt', 'heatcold'],
    'stroke from the heat': ['hurt', 'heatcold'], 'stroke in the heat': ['hurt', 'heatcold'],   # heat phrases win over "stroke"
    'signs of a stroke': ['hurt', 'sudden'],
}
TOPS = sorted({*TOPS, *EMERGENCY_FIRST, *EMERGENCY_UNDISTURBED})

# Labor in the words people type. One word at a time, "water broke" found unsafe tap water, "the baby is coming" a
# hurricane and "contractions" nothing: [guide, choice] that comes first.
PREGNANT = ['health', 'pregnant']
LABOR = {q: PREGNANT for q in [
    'my water broke', 'water broke', 'her water broke', 'my water just broke', 'waters broke', 'her waters have broken',
    'the baby is coming', 'baby is coming', "the baby's coming", 'how to deliver a baby', 'delivering a baby at home',
    'giving birth at home', 'going into labour', 'in labour', 'contractions', 'having contractions',
    'contractions 5 minutes apart', 'childbirth', 'umbilical cord', 'cut the umbilical cord',
    'giving birth', 'in labor', 'going into labor',
    "my wife's water broke", 'wifes water broke', 'water broke 2 hours ago', 'contractions 5 mins apart',
    'contractions every 3 minutes', 'baby crowning', 'labour pains', 'umbilical cord around neck',
    'labour started at home', 'water broke call 911', 'contractions every five minutes', 'water broke pregnancy',
]}
# The same words about something else keep their first result; Not breathing still comes first.
NOT_LABOR = {
    'water main broke': ['water', 'unsafe'], 'water pipe broke': ['water', 'unsafe'], 'broke water heater': ['water', 'unsafe'],
    'baby choking': ['hurt', 'breathing'], 'baby not breathing': ['hurt', 'breathing'], 'baby fever': ['health', 'start'],
    'child not breathing': ['hurt', 'breathing'], 'hurricane coming': ['disaster', 'hurricane'],
    'extension cord': ['power', 'generator'], 'apartment': ['disaster', 'highrise-fire'],
    'unconscious and in labor': ['hurt', 'breathing'], 'water broke and she is unconscious': ['hurt', 'breathing'],
    'water has broken through the wall': ['water', 'unsafe'], 'our water has broken': ['water', 'unsafe'],
    'our water is broken': ['water', 'unsafe'], 'giving birth control pills': ['health', 'medicine'],
    'contractions while having a seizure': ['hurt', 'sudden'], 'cut head having a baby': ['hurt', 'bleeding'],
    'contractions in my neck': ['hurt', 'head'],
}
# A labor phrase among other words keeps its other meaning: the pregnancy choice must not come first.
NOT_LABOR_FIRST = [
    'minutes apart', 'take medicine 30 minutes apart', 'seizures 5 minutes apart', 'baby is coming down with a fever',
    'the baby is coming home from the hospital', 'baby coming in and out of consciousness', 'baby coming out of the car',
    'deliver the baby formula', 'deliver a baby goat', 'my water broke down', "grandma's water has broken",
    "the city's water broke down", 'premature ventricular contractions', 'spasms and contractions in the neck',
    'cramps and contractions every few minutes apart in legs', 'childbirth class', 'lightning baby is coming',
    'labour hours', 'labour help', 'need labour fast', 'labour first time', 'his water just broke', 'our water just broke',
    'deliver the baby to me', 'baby coming with me', 'baby coming from the hospital', 'water broke up',
    'the water just broke', 'water broke in the car', "the hospital's water has broken", 'emergency water broke',
    'the cord came out', 'the power cord came out', 'phone cord came out', 'the baby is coming from the hospital',
    'the baby is coming with her', 'water broke in the house', 'the baby is coming to my moms house',
    "the baby is coming to my mom's tonight", 'deliver the baby to her', 'contractions around the neck',
    'cord around neck', 'cord around the babys neck', 'baby is coming to the hospital now',
]
# A labor phrase lists only guides that find the search's other words: none here, so the pregnancy choice alone.
LABOR_ALONE = ['deliver a baby 108', 'contractions 5 minutes apart 108', 'someone is giving birth', 'giving birth care', 'contractions after birth', 'in labour after prenatal care']
# Bleeding in labor opens the pregnancy choice, whose readings cover it, but Bleeding stays in the list.
LABOR_BLEEDING = ['umbilical cord bleeding', 'contractions and bleeding', 'childbirth bleeding',
                  'my water broke and she is bleeding', 'bleeding alone right now last night contractions']
# Everyday wording (second review): filler and question words, people, places, and each moment of a birth.
LABOR_EVERYDAY = [
    'my water broke what should I do', 'her water broke we are at home', "my wife's water broke we're at home",
    'my water broke and im scared', 'my water broke but no contractions', 'i think my water just broke',
    'my waters have gone', 'did my water break or did i pee', 'our baby is coming', 'the baby is coming and no one can come',
    'baby on the way', 'contractions are 4 min apart', 'contractions 10 minutes apart should we go to the hospital',
    'contractions in my back', 'how do i know if im in labor', "i'm in labor and i don't know what to do",
    'going through labor at home alone', 'baby is crowning what do i do', 'baby born at home',
    'the head is out what now', 'baby is breech', 'how to cut the umbilical cord', "placenta won't come out", 'bloody show',
    'how to give birth at home', 'my water broke during the hurricane', 'my water broke and the power is out',
    'in labour no midwife', 'labour pains every 5 minutes', "she's about to give birth", 'had the baby at home what now',
    'MY WATER BROKE!!!', 'my water broke in the bathroom', 'my sisters water broke', 'pregnant during the flood',
    'my water broke what am i supposed to do', 'her water broke we r at home', 'pretty sure my water broke',
    'my water broke now she is pushing', 'baby is coming out of her', 'what to do when your water breaks',
    'her water broke in the car', 'my water broke at work',
    'going through labour', 'her waters break', 'labor pain relief', "contractions won't stop", 'the cord came out first',
    'labour has started', 'signs of labour',
    'my water broke what should i bring', 'her water broke we are at a friends house',
    'her water broke and we live far from the hospital', "her water broke we can't reach the hospital",
]
# Every phrase of the pregnancy choice opens it on its own. Next to a life threat, the first guide is the one the search
# gets with the choice's phrase fields taken out: a phrase never changes what comes first (Breathing, Bleeding, or
# whatever main shows, even when that is not first aid).
PREGNANT_CHOICE = next(c for t in TOPICS if t['id'] == 'health' for c in t['choices'] if c['id'] == 'pregnant')
PHRASES_ALONE = [*PREGNANT_CHOICE['phrases'], *PREGNANT_CHOICE.get('weak', []),
                 *(f'my {p}' for p in PREGNANT_CHOICE.get('weak', []) if p.startswith('water'))]
THREATS = ['not breathing', 'unconscious', 'choking', 'having a seizure', 'chest pain', 'bleeding heavily',
           'heavy bleeding', 'a lot of blood', "bleeding won't stop", 'bleeding wont stop', "won't stop bleeding",
           'overdose', 'poisoned']
PHRASE_THREATS = sorted({f'{p} {t}' for p in PHRASES_ALONE for t in THREATS} | {f'{t} {p}' for p in PHRASES_ALONE[::7] for t in THREATS})
PLAIN_TOPICS = [{**t, 'choices': [{k: v for k, v in c.items() if k not in ('phrases', 'weak', 'who', 'context', 'unless', 'alongside')}
                                  for c in t.get('choices', [])]} for t in TOPICS]
NOT_LABOR_NOWHERE = ['delivery', 'deliver food', 'baby formula', 'minutes', 'power cord']   # no pregnancy guide for these
TOPS = sorted({*TOPS, *LABOR, *NOT_LABOR, *NOT_LABOR_NOWHERE, *NOT_LABOR_FIRST, *LABOR_BLEEDING, *LABOR_ALONE,
               *LABOR_EVERYDAY, *PHRASES_ALONE,
               *PHRASE_THREATS})

# A guide whose urgent phrase the search names comes first, even when the other words pull another guide ahead or
# below the cut; that guide is still offered: query -> (first row, a row that must still be shown).
GUIDE_URGENT = {
    'unconscious in the flood water': (['hurt', 'breathing'], ['water', 'unsafe']),
    'unconscious after drinking flood water': (['hurt', 'breathing'], ['water', 'unsafe']),
    'not breathing after falling in the well water': (['hurt', 'breathing'], ['land', 'water-source']),
    'unconscious after the generator ran indoors': (['hurt', 'breathing'], ['shelter', 'co']),
    'child unconscious after pet food': (['hurt', 'breathing'], None), 'pet food unconscious': (['hurt', 'breathing'], None),
}
# Searches without an urgent phrase keep their guide order: [guide, choice] that comes first.
GUIDE_ORDINARY = {
    'how do i light a campfire': ['power', 'first-hours'], 'treat water': ['water', 'keepclean'],
    "make the water safe for a baby's formula": ['food', 'outage'], 'not responding to antibiotics': ['health', 'medicine'],
    'radio not responding': ['comms', 'alerts-radio'], 'dental abscess': ['health', 'teeth'], 'propane burns': ['hurt', 'shock'],
}
TOPS = sorted({*TOPS, *GUIDE_URGENT, *GUIDE_ORDINARY})
# Cardiac arrest is urgent too: "heart stopped" in a search with other words still opens CPR first, and the guide
# those words point to is still offered: query -> (first row, a row that must still be shown).
HEART_URGENT = {
    'heart stopped in the flood water': (['hurt', 'breathing'], ['water', 'unsafe']),
    'heart stopped after drinking flood water': (['hurt', 'breathing'], ['water', 'unsafe']),
    'heart stopped after the generator ran indoors': (['hurt', 'breathing'], ['shelter', 'co']),
    'her heart has stopped': (['hurt', 'breathing'], None), 'cardiac arrest in the flood water': (['hurt', 'breathing'], None),
}
# Searches with "heart" or "stopped" but no cardiac arrest keep their first row.
HEART_ORDINARY = {
    'heart attack': ['hurt', 'sudden'], 'heart attack after shoveling snow': ['hurt', 'sudden'],
    'heart disease': ['health', 'chronic'], 'the generator stopped': ['power', 'generator'],
    'water heater stopped': ['water', 'inhome'],
}
TOPS = sorted({*TOPS, *HEART_URGENT, *HEART_ORDINARY})
# "cell service" is a phrase of the phones-down choice, so it counts only when the rest of the search is everyday
# words. A search that also asks about something else keeps every row it had: query -> all the rows shown, in order.
CELL_SERVICE_ROWS = {
    'no cell service': [['comms', 'phones-down']], 'lost cell service at home': [['comms', 'phones-down']],
    'no cell service cant call 911': [['comms', 'phones-down'], ['hurt', None]],
    'cant call 911': [['comms', 'phones-down'], ['hurt', None]],
    'no cell service how do i call for help': [['comms', 'phones-down'], ['hurt', 'nohelp']],
    'solar cell': [['power', 'batteries-solar']], 'cell phone battery': [['power', 'batteries-solar']],
}
TOPS = sorted({*TOPS, *CELL_SERVICE_ROWS})

# Another form of a word a guide already has: a plural, -ed or -ing. Alone, each found nothing, because a typed word
# must start a guide's word and "tourniquets" is longer than "tourniquet". A search for the first must give the same
# guides and choices, in the same order, as a search for the second.
FORMS = {
    'tourniquets': 'tourniquet', 'fainting': 'fainted', 'choked': 'choking', 'choke': 'choking',
    'drowned': 'drowning', 'bleeds': 'bleeding', 'wounds': 'wound', 'seizures': 'seizure', 'snakes': 'snake',
    'bees': 'bee', 'earthquakes': 'earthquake', 'evacuating': 'evacuate', 'boiled': 'boil', 'filters': 'filter',
}
# Whole searches in which no word starts a guide's word: [guide, choice] that comes first.
FORMS_FIRST = {
    'he is fainting': ['hurt', 'breathing'], 'kid choked on a grape': ['hurt', 'breathing'],
    'applying tourniquets': ['hurt', 'bleeding'], 'swarm of bees': ['hurt', 'sting'],
}
TOPS = sorted({*TOPS, *FORMS, *FORMS.values(), *FORMS_FIRST})

# The rule itself, on made-up topics so it doesn't depend on what the guides say: query -> ids found, in order.
# Only a search that finds nothing is tried by form, so "baby choked" still gives what "baby" gives.
FORM_TOPICS = [
    {'id': 'words', 'title': 'Tourniquet, fainted, choking, stopped, batteries, stitches', 'short': 'A plan for incoming missiles',
     'keywords': ['can', 'bee'], 'choices': []},
    {'id': 'baby', 'title': 'Baby care', 'short': 'Feeding', 'keywords': [], 'choices': []},
]
RANK_FORMS = {
    'tourniquets': ['words'], 'fainting': ['words'], 'faints': ['words'], 'choked': ['words'], 'choke': ['words'],
    'stopping': ['words'], 'battery': ['words'], 'stitching': ['words'], 'plans': ['words'], 'planning': ['words'],
    'bees': ['words'], 'missile': ['words'], 'he is fainting': ['words'], 'babies': ['baby'],
    'missing': [], 'planes': [], 'cans': [], 'stung': [],         # not missiles, not a plan, "can" is a stop word, irregular
    'baby': ['baby'], 'baby choked': ['baby'], 'choked baby': ['baby'], 'feeding tourniquets': ['baby'],
}

# Words used in Italy. hailstorm sits in the thunderstorm choice's hint and opens the guide hail opens. 118 was left
# out: it is an ambulance number in Italy but a fire number in Switzerland, and search has no country to tell apart.
IT_SAME_GUIDE = {'hailstorm': 'hail', 'hailstorms': 'hail', 'Hailstorm': 'hail'}
# Searches those words must not disturb: [guide, choice] that comes first. "water cut" was left out for the third:
# as a keyword of the water guide it put that guide ahead of first aid for any search with "water" and "cut" in it.
IT_UNDISTURBED = {'hail': ['disaster', 'thunderstorm'], 'thunderstorm': ['disaster', 'thunderstorm'],
                  'water on a cut': ['hurt', 'bleeding'],
                  'hailstorm': ['disaster', 'thunderstorm'], 'hailstorm damage': ['shelter', 'damaged']}
# Left out, so these still find nothing: "civil protection" as two keywords would let each word open the disaster
# guide alone, and "combi" (the boiler that heats most Italian homes) would answer "comb".
IT_NOTHING = ['civil', 'protection', 'comb']
TOPS = sorted({*TOPS, *IT_SAME_GUIDE, *IT_SAME_GUIDE.values(),
               *IT_UNDISTURBED, *IT_NOTHING})

# Avalanche, landslide and volcano situations in the disaster guide: [guide, choice] that comes first. Most found
# nothing before; "caught in avalanche" opened the tornado choice and "rock fall" the nuclear one.
SLIDES_AND_ERUPTIONS = {
    'avalanche': ['disaster', 'avalanche'], 'caught in avalanche': ['disaster', 'avalanche'],
    'avalanche warning': ['disaster', 'avalanche'], 'buried in snow': ['disaster', 'avalanche'],
    'landslide': ['disaster', 'landslide'], 'landslide warning': ['disaster', 'landslide'],
    'mudslide': ['disaster', 'landslide'], 'debris flow': ['disaster', 'landslide'],
    'rockfall': ['disaster', 'landslide'], 'rock fall': ['disaster', 'landslide'],
    'volcano': ['disaster', 'volcano'], 'volcano warning': ['disaster', 'volcano'],
    'eruption': ['disaster', 'volcano'], 'lava flow': ['disaster', 'volcano'],
    'volcanic ash': ['disaster', 'volcano'], 'ashfall': ['disaster', 'volcano'],      # clearly volcanic
}
# Searches those situations must not disturb: [guide, choice] that comes first. Ash on its own is smoke, not a
# volcano: "ash fall" has "fall", which also starts "fallout", and put Nuclear explosion first while "ashfall" was a
# keyword of the disaster guide. "snow" stays with shelter, a flood clean-up with sanitation, someone trapped still
# gets signalling for help, and "back" does not find a word in the avalanche hint.
SLIDES_UNDISTURBED = {
    'ash': ['shelter', 'smoke'], 'ash fall': ['shelter', 'smoke'], 'ash falling': ['shelter', 'smoke'],
    'snow': ['shelter', None], 'mud in the house': ['sanitation', 'floodmess'],
    'trapped under debris': ['comms', 'signal'], 'tsunami warning': ['disaster', 'tsunami'],
    'back labour poisoned': ['hurt', 'head'],
}
TOPS = sorted({*TOPS, *SLIDES_AND_ERUPTIONS, *SLIDES_UNDISTURBED})

# Three short words that start a longer word, from the list in #333 and the benchmark in #430: "rat" starts
# "rattlesnake" in the snakebite hint, and "out" starts "outage" in the fridge-and-freezer choice. Phrases on the
# choice that answers each search fix them without touching the matching: [guide, choice] that comes first.
OUT_AND_RAT = {
    'rat bite': ['hurt', 'animal-bite'], 'rat bites': ['hurt', 'animal-bite'],
    'bitten by a rat': ['hurt', 'animal-bite'], 'rodent bite': ['hurt', 'animal-bite'],
    'out of formula': ['food', 'special'], 'out of baby formula': ['food', 'special'],
    'running out of formula': ['food', 'special'], 'running low on formula': ['food', 'special'],
    'out of insulin': ['health', 'chronic'], 'ran out of insulin': ['health', 'chronic'],
    'running out of insulin': ['health', 'chronic'], 'running low on insulin': ['health', 'chronic'],
    'out of my prescriptions': ['health', 'chronic'], 'ran out of my medication': ['health', 'chronic'],
}
# Searches those phrases must not disturb. "out of" is everyday wording for running low on anything, and the fridge
# choice is still the right answer for food; a snake bite is still a snake bite; rats in the rubbish are sanitation.
OUT_AND_RAT_UNDISTURBED = {
    'out of food': ['food', 'outage'], 'running out of food': ['food', 'outage'],
    'out of water': ['hurt', 'diarrhea'], 'ran out of water': ['hurt', 'diarrhea'],
    'out of gas': ['power', 'first-hours'], 'power outage': ['shelter', 'co'],
    'snake bite': ['hurt', 'snakebite'], 'rattlesnake': ['hurt', 'snakebite'],
    'rats': ['sanitation', 'garbage'],   # "rat poison" is not pinned here: #331 moves it to the poison choice
    'formula': ['food', 'special'], 'insulin': ['health', 'chronic'],
}
TOPS = sorted({*TOPS, *OUT_AND_RAT, *OUT_AND_RAT_UNDISTURBED})

# #449: "bulging ground" is the right wording for a landslide, but a hint word counts on its own, so every search
# holding "bulging" reached the landslide choice -- a swollen tin, which is the botulism warning, and "bulging disc",
# which is a back injury. Every form of the warning sign is a phrase now, and no form of "bulge" is in the hint at
# all: putting "bulges" there instead only moved the same bug onto "can bulges" and "disc bulges".
# [guide, choice] that must come first.
BULGING = {
    'bulging ground': ['disaster', 'landslide'], 'ground bulging': ['disaster', 'landslide'],
    'ground bulges': ['disaster', 'landslide'], 'slope bulges': ['disaster', 'landslide'],
    'ground cracking': ['disaster', 'landslide'], 'cracking ground': ['disaster', 'landslide'],
    'ground rumbling': ['disaster', 'landslide'], 'mudflow': ['disaster', 'landslide'],
    'mudslide injured': ['disaster', 'landslide'], 'rock slide': ['disaster', 'landslide'],
    'debris flow': ['disaster', 'landslide'], 'rockfall': ['disaster', 'landslide'],
    'bulging can': ['preserve', 'jarcheck'], 'can is bulging': ['preserve', 'jarcheck'],
    'bulging cans': ['food', 'throwout'], 'bulging lid': ['preserve', 'jarcheck'],
}
# Searches that must not reach the landslide choice at all, anywhere in the three results. Where "bulging disc"
# does rank is a separate question, so it is not pinned to a result; "cracks" guards the wording of the new hint.
NOT_LANDSLIDE = ['bulging can', 'bulging cans', 'can is bulging', 'bulging disc', 'bulging',
                 'can bulges', 'the can bulges', 'disc bulges', 'bulges',
                 'cracks in the wall', 'cracked wall', 'cracks', 'cracks in the foundation']
TOPS = sorted({*TOPS, *BULGING, *NOT_LANDSLIDE})

# A poisoning is usually told as drinking something, not swallowing it, and the question that follows is whether to
# make the person vomit. The poison choice's own readings answer both ("Do not induce vomiting or give milk, water
# or charcoal"), but on 51658f5 no guide held "drank", so a child drinking fuel reached the small-engine guide,
# "my son drank bleach" reached the guide on disinfecting drinking water, and "induce vomiting" reached shock and
# diarrhoea. The words live in "phrases", gated by "context", not in the hint.
DRANK_AND_VOMIT = {
    'my son drank bleach': ['hurt', 'poison'], 'my daughter drank bleach': ['hurt', 'poison'],
    'kid drank bleach': ['hurt', 'poison'], 'my son drank gasoline': ['hurt', 'poison'],
    'he drank petrol': ['hurt', 'poison'], 'she drank kerosene': ['hurt', 'poison'],
    'drank antifreeze': ['hurt', 'poison'], 'drank detergent': ['hurt', 'poison'],
    'drank weed killer': ['hurt', 'poison'], 'induce vomiting': ['hurt', 'poison'],
    'should i make him vomit': ['hurt', 'poison'], 'make him throw up': ['hurt', 'poison'],
    'give activated charcoal': ['hurt', 'poison'],
    'activated charcoal for poisoning': ['hurt', 'poison'],
}
# Searches those phrases must not disturb: bleach and charcoal are the water guide's when the search is about
# treating water, and an ant bite is still a bite.
DRANK_UNDISTURBED = {
    'bleach': ['water', 'bleach'], 'household bleach': ['water', 'bleach'],
    'how much bleach to purify water': ['water', 'keepclean'],
    'activated charcoal water filter': ['water', 'unsafe'], 'charcoal filter': ['water', 'filter'],
    'ant bites': ['health', 'medicine'], 'swallowed detergent': ['hurt', 'poison'],
}
# "weed killer" and "antifreeze" are phrases, never hint words: a hint word is matched by prefix, so "killer" in the
# hint would send every "kill" search to the poison choice and "antifreeze" would catch "ant". An animal that drank
# something is not this choice either -- the guide for a person who swallowed a poison is the wrong page for a dog --
# so the pet words are in "unless", which switches the phrases off. And "activated charcoal" on its own is a water
# filter as often as a poisoning, so only the phrases that name the poisoning reach the choice. None of these are
# pinned to a result: #328 gives the crisis searches one, and where a pet search should land is its own question.
DRANK_NOT_POISON = ['kill myself', 'i want to kill myself', 'she wants to kill herself',
                    'kill weeds', 'kill bacteria', 'how to kill a chicken', 'ants in the kitchen',
                    'my dog drank bleach', 'my cat drank antifreeze', 'the dog drank antifreeze',
                    'activated charcoal', 'activated charcoal water filter']
TOPS = sorted({*TOPS, *DRANK_AND_VOMIT, *DRANK_UNDISTURBED, *DRANK_NOT_POISON})

CASES = {
    'stop': QSTOP, 'fold': list(FOLD), 'match': [m[:2] for m in MATCH], 'rank': ENGLISH, 'topics': TOPICS,
    'rankOwn': RANK_OWN, 'rankForms': [[q, FORM_TOPICS] for q in RANK_FORMS],
    'words': list(WORDS_BOTH) + ['怎么净化水', '水をきれいにする方法',
                                 '물 끓이기', 'น้ำ'],
    'tops': TOPS,
}

CASES.update(topsPlain=PHRASE_THREATS, plainTopics=PLAIN_TOPICS)   # the same searches without the phrase fields


def run(mode):
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / 'cases.json'
        p.write_text(json.dumps(CASES, ensure_ascii=False), encoding='utf-8')
        r = subprocess.run([NODE, str(HERE / 'topic_search_test.mjs'), str(WORDS), str(p), mode],
                           capture_output=True, text=True, encoding='utf-8', timeout=60)
    if r.returncode:
        raise AssertionError(r.stderr[-1500:])
    return json.loads(r.stdout)


def ok(entry):
    if 'error' in entry:
        raise AssertionError(entry['error'])
    return entry['ok']


@unittest.skipUnless(NODE, 'needs node on PATH')
class TopicSearch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.seg, cls.wk, cls.fb, cls.thr = run('segmenter'), run('webkit'), run('fallback'), run('throwing')
        cls.modes = (cls.seg, cls.wk, cls.fb, cls.thr)

    def words(self, r):
        return dict(zip(CASES['words'], (ok(e) for e in r['words'])))

    def test_modes(self):
        self.assertTrue(self.seg['segmenter'])
        self.assertTrue(self.wk['segmenter'])
        self.assertFalse(self.fb['segmenter'])
        self.assertFalse(self.thr['segmenter'])
        self.assertEqual(self.thr, self.fb)                       # a throwing Segmenter falls back cleanly

    def test_fold(self):
        for r in self.modes:
            self.assertEqual(dict(zip(FOLD, r['fold'])), FOLD)

    def test_turkish_i_order(self):
        # Shows why the order matters: removing marks from NFC "İ" finds none, and lower-casing it afterwards
        # leaves "i" + U+0307, which no longer matches "istanbul". The fold assertions check words.js itself.
        marks_first = ''.join(ch for ch in unicodedata.normalize('NFC', 'İ') if not unicodedata.combining(ch)).lower()
        self.assertEqual(marks_first, 'i̇')
        for r in self.modes:
            self.assertEqual(r['fold'][0], 'istanbul')

    def test_query_words(self):
        for r in self.modes:
            w = self.words(r)
            for q, expect in WORDS_BOTH.items():
                with self.subTest(q=q, segmenter=r['segmenter']):
                    self.assertEqual(w[q], expect)

    def test_scripts_without_spaces(self):
        for r in (self.seg, self.wk):
            w = self.words(r)
            zh = w['怎么净化水']
            self.assertEqual(''.join(zh), '怎么净化水')      # nothing dropped
            self.assertTrue(all(1 <= len(x) <= 2 for x in zh))                  # one- and two-character words count
            ja = w['水をきれいにする方法']
            self.assertIn('水', ja)
            self.assertIn('方法', ja)
            self.assertIn('물', w['물 끓이기'])              # Korean 물 (water), one syllable
            self.assertTrue(w['น้ำ'])                            # Thai น้ำ (water)
        for r in (self.fb, self.thr):                                           # no segmenter: the run stays whole
            self.assertEqual(self.words(r)['怎么净化水'], ['怎么净化水'])

    def test_match(self):
        for r in self.modes:
            self.assertEqual([ok(e) for e in r['match']], [m[2] for m in MATCH])

    def test_emergency_words_open_first_aid(self):
        for r in self.modes:
            top = dict(zip(TOPS, (ok(e) for e in r['tops'])))
            for q, first in [*EMERGENCY_FIRST.items(), *EMERGENCY_UNDISTURBED.items()]:
                with self.subTest(q=q, segmenter=r['segmenter']):
                    self.assertEqual(top[q][:1], [first])   # an empty result fails, not errors

    def test_rank_non_english_topic(self):
        for r in self.modes:
            self.assertEqual([ok(e) for e in r['rankOwn']], [[['own', None]]] * 3)

    def test_everyday_words_find_their_guide(self):
        for r in self.modes:
            top = dict(zip(TOPS, (ok(e) for e in r['tops'])))
            for q, first in EVERYDAY.items():
                with self.subTest(q=q, segmenter=r['segmenter']):
                    self.assertEqual(top[q][0] if top[q] else None, first)
            for q, choice in LISTED.items():
                with self.subTest(q=q, segmenter=r['segmenter']):
                    self.assertIn(choice, top[q])

    def test_english_ranks_as_before(self):
        for r in self.modes:
            for case in r['rank']:
                with self.subTest(q=case['q'], segmenter=r['segmenter']):
                    self.assertEqual(ok(case['now']), case['before'])
        self.assertGreater(sum(1 for c in self.seg['rank'] if c['before']), 30)   # the list does reach the guides

    def test_words_used_in_thailand_and_denmark(self):
        for r in self.modes:
            top = dict(zip(TOPS, (ok(e) for e in r['tops'])))
            for q, us in TH_DK_SAME_AS.items():
                with self.subTest(q=q, us=us, segmenter=r['segmenter']):
                    self.assertTrue(top[us])
                    self.assertEqual(top[q], top[us])
            for q, us in TH_DK_SAME_GUIDE.items():
                with self.subTest(q=q, us=us, segmenter=r['segmenter']):
                    self.assertTrue(top[q])
                    self.assertEqual(top[q][0][0], top[us][0][0])
            for q, first in TH_DK_UNDISTURBED.items():
                with self.subTest(q=q, segmenter=r['segmenter']):
                    self.assertEqual(top[q][:1], [first])
            # "burning season" was left out for these two, so neither opens the shelter guide on its own. "burning"
            # is now found by word form, where "burned" already was.
            for q, found in (('burning', [['hurt', 'burn']]), ('season', [])):
                with self.subTest(q=q, segmenter=r['segmenter']):
                    self.assertEqual(top[q], found)
            for q in NOT_FIRST_AID:                             # a fire number does not open the hurt guide
                with self.subTest(q=q, segmenter=r['segmenter']):
                    self.assertNotIn('hurt', [guide for guide, _ in top[q]])

    def test_words_used_outside_the_us(self):
        for r in self.modes:
            top = dict(zip(TOPS, (ok(e) for e in r['tops'])))
            for q, us in SAME_AS.items():
                with self.subTest(q=q, us=us, segmenter=r['segmenter']):
                    self.assertTrue(top[us])                       # the US word does find a guide
                    self.assertEqual(top[q], top[us])
            for q, us in SAME_GUIDE.items():
                with self.subTest(q=q, us=us, segmenter=r['segmenter']):
                    self.assertTrue(top[q])
                    self.assertEqual(top[q][0][0], top[us][0][0])
            for q, first in [*FIRST.items(), *ALREADY_COVERED.items(), *URGENT_FIRST.items(), *UNDISTURBED.items(), *PAIN_NOT_PAINT.items()]:
                with self.subTest(q=q, segmenter=r['segmenter']):
                    self.assertEqual(top[q][0], first)

    def test_other_forms_of_a_word(self):
        for r in self.modes:
            found = dict(zip(RANK_FORMS, ([row[0] for row in ok(e)] for e in r['rankForms'])))
            for q, ids in RANK_FORMS.items():
                with self.subTest(q=q, segmenter=r['segmenter']):
                    self.assertEqual(found[q], ids)
            top = dict(zip(TOPS, (ok(e) for e in r['tops'])))
            for q, word in FORMS.items():
                with self.subTest(q=q, word=word, segmenter=r['segmenter']):
                    self.assertTrue(top[word])                     # the guide's own word does find a guide
                    self.assertEqual(top[q], top[word])
            for q, first in FORMS_FIRST.items():
                with self.subTest(q=q, segmenter=r['segmenter']):
                    self.assertEqual(top[q][:1], [first])

    def test_urgent_choice_wins(self):
        for r in self.modes:
            top = dict(zip(TOPS, (ok(e) for e in r['tops'])))
            for q, first in URGENT.items():
                with self.subTest(q=q, segmenter=r['segmenter']):
                    self.assertEqual(top[q][0] if top[q] else None, first)

    def test_breathing_after_a_sting(self):
        for r in self.modes:
            top = dict(zip(TOPS, (ok(e) for e in r['tops'])))
            for q in NOT_STING:
                with self.subTest(q=q, segmenter=r['segmenter']):
                    self.assertTrue(top[q])
                    self.assertNotIn(['hurt', 'sting'], top[q])

    def test_urgent_guide_comes_first(self):
        for r in self.modes:
            top = dict(zip(TOPS, (ok(e) for e in r['tops'])))
            for q, (first, offered) in GUIDE_URGENT.items():
                with self.subTest(q=q, segmenter=r['segmenter']):
                    self.assertEqual(top[q][:1], [first])
                    if offered:
                        self.assertIn(offered, top[q])
                    self.assertLessEqual(len(top[q]), 3)
            for q, first in GUIDE_ORDINARY.items():
                with self.subTest(q=q, segmenter=r['segmenter']):
                    self.assertEqual(top[q][:1], [first])

    def test_heart_stopped_is_urgent(self):
        for r in self.modes:
            top = dict(zip(TOPS, (ok(e) for e in r['tops'])))
            for q, (first, offered) in HEART_URGENT.items():
                with self.subTest(q=q, segmenter=r['segmenter']):
                    self.assertEqual(top[q][:1], [first])
                    if offered:
                        self.assertIn(offered, top[q])
            for q, first in HEART_ORDINARY.items():
                with self.subTest(q=q, segmenter=r['segmenter']):
                    self.assertEqual(top[q][:1], [first])


    def test_her_ranks_as_him(self):
        for r in self.modes:
            top = dict(zip(TOPS, (ok(e) for e in r['tops'])))
            for q, him in SAME_FOR_HIM.items():
                with self.subTest(q=q, segmenter=r['segmenter']):
                    self.assertTrue(top[him])                      # the question about him does find a guide
                    self.assertEqual(top[q], top[him])
            for q, first in STILL_SHELTER.items():
                with self.subTest(q=q, segmenter=r['segmenter']):
                    self.assertEqual(top[q][:1], [first])

    def test_labor_finds_the_pregnancy_guide(self):
        for r in self.modes:
            top = dict(zip(TOPS, (ok(e) for e in r['tops'])))
            for q, first in {**LABOR, **NOT_LABOR}.items():
                with self.subTest(q=q, segmenter=r['segmenter']):
                    self.assertEqual(top[q][:1], [first])
            for q in NOT_LABOR_NOWHERE:
                with self.subTest(q=q, segmenter=r['segmenter']):
                    self.assertNotIn(PREGNANT, top[q])
            for q in NOT_LABOR_FIRST:
                with self.subTest(q=q, segmenter=r['segmenter']):
                    self.assertNotEqual(top[q][:1], [PREGNANT])
            for q in LABOR_BLEEDING:
                with self.subTest(q=q, segmenter=r['segmenter']):
                    self.assertEqual(top[q][:1], [PREGNANT])
                    self.assertIn(['hurt', 'bleeding'], top[q])
            for q in LABOR_ALONE:
                with self.subTest(q=q, segmenter=r['segmenter']):
                    self.assertEqual(top[q], [PREGNANT])
            for q in [*LABOR_EVERYDAY, *PHRASES_ALONE]:
                with self.subTest(q=q, segmenter=r['segmenter']):
                    self.assertEqual(top[q][:1], [PREGNANT])

    def test_cell_service_keeps_the_other_rows(self):
        for r in self.modes:
            top = dict(zip(TOPS, (ok(e) for e in r['tops'])))
            for q, rows in CELL_SERVICE_ROWS.items():
                with self.subTest(q=q, segmenter=r['segmenter']):
                    self.assertEqual(top[q], rows)

    def test_labor_and_labour_alike(self):
        # Every phrase spelled one way has its twin in the other spelling.
        for field in ('phrases', 'weak'):
            entries = set(PREGNANT_CHOICE.get(field, []))
            for p in entries:
                twin = p.replace('labour', 'labor') if 'labour' in p else p.replace('labor', 'labour')
                with self.subTest(field=field, phrase=p):
                    self.assertIn(twin, entries)

    def test_context_never_holds_a_life_threat(self):
        # A word in "context" lets a phrase count next to it, so a life threat there would put the pregnancy choice
        # ahead of first aid. The main words of every urgent phrase ("breathing", not "out"), and these, stay out.
        threats = {w for t in TOPICS for c in t.get('choices', []) for u in c.get('urgent', []) for w in u.split()
                   if len(w) > 3 and w not in ("isn't", 'isnt')}
        threats |= {'bleeding', 'blood', 'loss', 'weak', 'moving', 'limp', 'heavily', 'heavy', 'hemorrhage',
                    'haemorrhage', 'seizure', 'fitting', 'convulsing', 'choking', 'breathe', 'pulse', 'chest',
                    'overdose', 'poison', 'poisoned', 'shock', 'faint', 'fainted', 'blue', 'pale', 'stroke'}
        for entry in [*PREGNANT_CHOICE.get('context', []), *PREGNANT_CHOICE.get('alongside', [])]:
            with self.subTest(entry=entry):
                self.assertFalse(set(entry.split()) & threats)

    def test_a_life_threat_ranks_as_without_phrases(self):
        for r in self.modes:
            top = dict(zip(TOPS, (ok(e) for e in r['tops'])))
            plain = dict(zip(PHRASE_THREATS, (ok(e) for e in r['topsPlain'])))
            for t in THREATS:
                with self.subTest(q=t, segmenter=r['segmenter']):
                    self.assertTrue(plain[f'{PHRASES_ALONE[0]} {t}'])
            for q in PHRASE_THREATS:
                with self.subTest(q=q, segmenter=r['segmenter']):
                    self.assertEqual(top[q][:1], plain[q][:1])

    def test_words_used_in_italy(self):
        for r in self.modes:
            top = dict(zip(TOPS, (ok(e) for e in r['tops'])))
            for q, us in IT_SAME_GUIDE.items():
                with self.subTest(q=q, us=us, segmenter=r['segmenter']):
                    self.assertTrue(top[q])
                    self.assertEqual(top[q][0][0], top[us][0][0])
            for q, first in IT_UNDISTURBED.items():
                with self.subTest(q=q, segmenter=r['segmenter']):
                    self.assertEqual(top[q][:1], [first])
            for q in IT_NOTHING:
                with self.subTest(q=q, segmenter=r['segmenter']):
                    self.assertEqual(top[q], [])


    def test_out_of_and_rat_reach_the_choice_that_answers_them(self):
        for r in self.modes:
            top = dict(zip(TOPS, (ok(e) for e in r['tops'])))
            for q, first in {**OUT_AND_RAT, **OUT_AND_RAT_UNDISTURBED}.items():
                with self.subTest(q=q, segmenter=r['segmenter']):
                    self.assertEqual(top[q][:1], [first])   # an empty result fails, not errors

    def test_bulging_reaches_the_landslide_choice_only_about_the_ground(self):
        for r in self.modes:
            top = dict(zip(TOPS, (ok(e) for e in r['tops'])))
            for q, first in BULGING.items():
                with self.subTest(q=q, segmenter=r['segmenter']):
                    self.assertEqual(top[q][:1], [first])   # an empty result fails, not errors
            for q in NOT_LANDSLIDE:
                with self.subTest(q=q, segmenter=r['segmenter']):
                    self.assertNotIn(['disaster', 'landslide'], top[q])

    def test_drinking_a_poison_and_whether_to_make_someone_vomit(self):
        for r in self.modes:
            top = dict(zip(TOPS, (ok(e) for e in r['tops'])))
            for q, first in {**DRANK_AND_VOMIT, **DRANK_UNDISTURBED}.items():
                with self.subTest(q=q, segmenter=r['segmenter']):
                    self.assertEqual(top[q][:1], [first])   # an empty result fails, not errors
            for q in DRANK_NOT_POISON:
                with self.subTest(q=q, segmenter=r['segmenter']):
                    self.assertNotIn(['hurt', 'poison'], top[q])

    def test_avalanche_landslide_and_volcano(self):
        for r in self.modes:
            top = dict(zip(TOPS, (ok(e) for e in r['tops'])))
            for q, first in {**SLIDES_AND_ERUPTIONS, **SLIDES_UNDISTURBED}.items():
                with self.subTest(q=q, segmenter=r['segmenter']):
                    self.assertEqual(top[q][:1], [first])   # an empty result fails, not errors

if __name__ == '__main__':
    unittest.main()
