"""Read-only address lookup using the state indexes installed with offline maps."""
import math
import re
import sqlite3
import unicodedata
from pathlib import Path

from engine.paths import ro_uri

# Keep normalization compatible with tools/build_addresses.py.
WORDS = {
    'boulevard': 'blvd', 'avenue': 'ave', 'av': 'ave', 'street': 'st',
    'drive': 'dr', 'road': 'rd', 'place': 'pl', 'court': 'ct', 'lane': 'ln',
    'terrace': 'ter', 'circle': 'cir', 'highway': 'hwy', 'parkway': 'pkwy',
    'square': 'sq', 'north': 'n', 'south': 's', 'east': 'e', 'west': 'w',
    'mount': 'mt', 'saint': 'st',
}
STATE_NAMES = (
    'Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|'
    'District of Columbia|Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|'
    'Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|'
    'Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|'
    'New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|'
    'Pennsylvania|Puerto Rico|Rhode Island|South Carolina|South Dakota|'
    'Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming'
).split('|')
STATE_CODES = 'AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA PR RI SC SD TN TX UT VT VA WA WV WI WY'.split()
STATES = dict(zip((name.lower() for name in STATE_NAMES), STATE_CODES))
ONES = 'zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty'.split()
ORDINALS = 'zeroth first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth twentieth'.split()
TO_DIGITS = {word: str(number) for number, word in enumerate(ONES)}
for number, word in enumerate(ORDINALS):
    suffix = 'th' if 10 <= number % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(number % 10, 'th')
    TO_DIGITS[word] = f'{number}{suffix}'
TO_WORDS = {value: key for key, value in TO_DIGITS.items()}
# How a street can end. Three of these are also state codes: Ct (Court, Connecticut), Mt (Mount, Montana), NE (northeast, Nebraska).
STREET_ENDINGS = set(WORDS.values()) | {'ne', 'nw', 'se', 'sw'}
DIAGONALS = {'ne': 'northeast', 'nw': 'northwest', 'se': 'southeast', 'sw': 'southwest'}
DIAGONALS.update({full: short for short, full in DIAGONALS.copy().items()})


US_NORM = 'us'        # indexes without a meta 'norm' key: the published US state indexes and the legacy addresses.sqlite
DE_NORM = 'de-1'
_UMLAUTS = str.maketrans({'ä': 'ae', 'ö': 'oe', 'ü': 'ue', 'ß': 'ss'})


def normalize(text):
    return ' '.join(WORDS.get(word, word) for word in re.sub(r'[^a-z0-9 ]+', ' ', text.lower()).split())


def normalize_de(text):
    """Scheme 'de-1': 'Müllerstraße' -> 'muellerstr', 'Straße des 17. Juni' -> 'str des 17 juni'.

    German indexes store this in their norm column. tools/build_region_de_be.py keeps an identical copy, so the data
    half needs nothing from engine/; app/tests/test_address_search.py keeps the two equal."""
    text = unicodedata.normalize('NFC', text).lower().translate(_UMLAUTS)
    words = re.sub(r'[^a-z0-9 ]+', ' ', text).split()
    return ' '.join(word[:-len('strasse')] + 'str' if word.endswith('strasse') else word for word in words)


def is_address_query(query):
    """A standalone ZIP remains a place query. A number followed by words is an address."""
    return bool(re.match(r'^\s*\d[\w-]*\s+\S', query))


def is_german_address_query(query):
    """Street words, then a house number, then an optional postcode and place ('Unter den Linden 77, 10117 Berlin').

    This form also fits place names such as 'Route 66', so callers search places when no address matches."""
    return _parts_de(query) is not None


def _parts_de(query):
    """(number, street words, postcode, qualifier words) in German order, or None.

    The house number is the last number before an optional postcode and place, so 'Straße des 17. Juni 135' keeps
    its 17 in the street. A single letter after the number is its suffix: '12 a' -> '12A'."""
    tokens = normalize_de(query).split()
    end = len(tokens)
    while end and not re.search(r'\d', tokens[end - 1]):
        end -= 1
    head, qualifier = tokens[:end], tokens[end:]
    postcode = suffix = ''
    if len(head) >= 3 and re.fullmatch(r'\d{5}', head[-1]) and (
            re.fullmatch(r'\d+[a-z]?', head[-2]) or (re.fullmatch(r'[a-z]', head[-2]) and head[-3].isdigit())):
        postcode = head.pop()
    if head and head[-1].isdigit() and not postcode and qualifier and len(qualifier[0]) == 1:
        suffix = qualifier.pop(0)
    elif len(head) >= 2 and re.fullmatch(r'[a-z]', head[-1]) and head[-2].isdigit():
        suffix = head.pop()
    if not head or not re.fullmatch(r'\d+[a-z]?', head[-1]):
        return None
    number = (head.pop() + suffix).upper()
    if not any(re.search(r'[a-z]', word) for word in head):
        return None
    return number, head, postcode, qualifier


def is_german_postcode_query(query, geo_directory):
    """'10117 Berlin': a postcode and a place that an installed German index knows together. That is a place, even
    though it starts with a number like a US address; without a German index the answer is always False."""
    match = re.fullmatch(r'\s*(\d{5})[\s,]+([^\d]+)', query)
    if not match:
        return False
    postcode, qualifier = match.group(1), normalize_de(match.group(2))
    if not qualifier:
        return False
    for path in sorted((Path(geo_directory) / 'addresses').glob('*.sqlite')):
        if _index_info(path)[0] != DE_NORM:
            continue
        try:
            connection = sqlite3.connect(ro_uri(path), uri=True)
            try:
                found = connection.execute('SELECT DISTINCT city, district FROM addr WHERE zip = ?', (postcode,)).fetchall()
            finally:
                connection.close()
        except sqlite3.Error:
            continue
        if any(_place_matches(qualifier, city, district) for city, district in found):
            return True
    return False


def _place_matches(qualifier, city, district):
    """The place after a German number may name the city or the Ortsteil, prefix-matched like the US city filter."""
    city, district = city or '', district or ''
    places = (normalize_de(city), normalize_de(district), normalize_de(f'{city} {district}'))
    return any(place.startswith(qualifier) for place in places if place)


_INFO = {}


def _index_info(path):
    """(norm scheme, source line) from an index's meta table, cached until the file changes."""
    try:
        stat = path.stat()
    except OSError:
        return US_NORM, None
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    if key not in _INFO:
        meta = {}
        try:
            connection = sqlite3.connect(ro_uri(path), uri=True)
            try:
                meta = dict(connection.execute("SELECT key,value FROM meta WHERE key IN ('norm','source')"))
            finally:
                connection.close()
        except sqlite3.Error:
            pass        # no meta table: an index from before norm schemes, so US rules
        _INFO[key] = (meta.get('norm') or US_NORM, meta.get('source'))
    return _INFO[key]


def street_forms(street):
    words = street.split()
    forms = [street, ' '.join(TO_DIGITS.get(word, word) for word in words),
             ' '.join(TO_WORDS.get(word, word) for word in words)]
    forms += [re.sub(r'\b(\d+)(st|nd|rd|th)\b', r'\1', form) for form in forms[:]]
    # Existing indexes keep both spellings, so try each without changing their normalization.
    for position in (0, -1):
        for form in forms[:]:
            parts = form.split()
            if parts[position] in DIAGONALS:
                parts[position] = DIAGONALS[parts[position]]
                forms.append(' '.join(parts))
    if words[0] not in ('n', 's', 'e', 'w') and words[0] not in DIAGONALS:
        forms += [f'{direction} {form}' for form in forms[:] for direction in ('n', 's', 'e', 'w')]
    return list(dict.fromkeys(forms))


def _parts(query, states=True):
    number, remainder = query.strip().split(None, 1)
    number = number.upper()
    remainder = re.sub(r'[^a-z0-9 ]+', ' ', remainder.lower()).split()
    # A ZIP+4 ("62704-1234", or "62704 1234" once the hyphen is gone) is the same place as its five-digit ZIP, which the
    # index keeps: drop the four extra digits so the ZIP and the state before it are still read.
    if len(remainder) > 1 and re.fullmatch(r'\d{4}', remainder[-1]) and re.fullmatch(r'\d{5}', remainder[-2]):
        remainder.pop()
    postcode = ''
    if remainder and re.fullmatch(r'\d{5}', remainder[-1]):
        postcode = remainder.pop()
    state = ''
    for count in (3, 2, 1):
        if not states or len(remainder) <= count:
            continue
        name = ' '.join(remainder[-count:])
        code = STATES.get(name) or (name.upper() if count == 1 and name.upper() in STATE_CODES else '')
        if code:
            state = code
            remainder = remainder[:-count]
            break
    return number, normalize(' '.join(remainder)).split(), state, postcode, remainder


def _rank(near):
    def rank(row):
        distance = 0
        if near:
            distance = (row[5] - near[0]) ** 2 + ((row[6] - near[1]) * math.cos(math.radians(near[0]))) ** 2
        return len(row[7]), distance, row[3], row[2], row[1]
    return rank


def _readings(query):
    """The ways to read a query, likeliest first, as (number, words, state, postcode, plain, town).
    Words at the end that name a state can also be the street's own ending ('12 Oak Ct', '500 4th St NE') or a town
    ('Washington'), so the query is read both ways. The second reading stays narrow, so that a state that is not installed
    still finds nothing: a two-letter code is tried only as part of the street (town ''), and a state's name only as a town
    of exactly that name (town 'whole'). '100 Main St IN' does not find Inglewood, nor 'Maple Ave Kansas' Kansas City."""
    number, words, state, postcode, plain = _parts(query)
    if not state:
        return [(number, words, state, postcode, plain, 'starts')]
    typed = _parts(query, states=False)
    code = len(typed[1]) == len(words) + 1 and typed[1][-1].upper() in STATE_CODES
    by_state, as_typed = (number, words, state, postcode, plain, 'starts'), (*typed, '' if code else 'whole')
    return [as_typed, by_state] if code and typed[1][-1] in STREET_ENDINGS else [by_state, as_typed]


def find_addresses(query, geo_directory, limit=8, near=None):
    """Return exact house-number matches from installed indexes, never a guessed house.

    A number-first query searches the US-normalized indexes; a German-order query ('Müllerstr. 12a') searches the
    indexes whose meta norm is de-1. Each index is only ever searched with its own normalization. A number-first
    query that no US index answers may still name a German house ('77 Unter den Linden'), so the de-1 indexes are
    asked last, for that street in full."""
    if is_address_query(query):
        return (_find_us(query, Path(geo_directory), limit, near)
                or _find_de_number_first(query, Path(geo_directory), limit, near))
    parts = _parts_de(query)
    if parts is None:
        return []
    return _find_de(parts, Path(geo_directory), limit, near)


def _parts_de_number_first(query):
    """(number, words, postcode, qualifier words) for a German house written number first ('77 Unter den Linden',
    '12a Müllerstraße, 13353 Berlin'), or None. The words up to a postcode are the street; without a postcode, where
    the street ends is left to _find_de_number_first."""
    tokens = normalize_de(query).split()
    if len(tokens) < 2 or not re.fullmatch(r'\d+[a-z]?', tokens[0]):
        return None
    number, words, postcode, qualifier = tokens[0].upper(), tokens[1:], '', []
    for position, token in enumerate(words):
        if re.fullmatch(r'\d{5}', token):
            words, postcode, qualifier = words[:position], token, words[position + 1:]
            break
    return number, words, postcode, qualifier


def _find_de_number_first(query, geo_directory, limit, near):
    """The street must be one stored street in full, not the start of one: '77 Linden' is a US query and must not find
    Lindenstraße, nor '100 Main' Mainzer Straße. Without a postcode the longest such street wins, and the words after
    it must name the place, as in '77 Unter den Linden Berlin'."""
    parts = _parts_de_number_first(query)
    if parts is None:
        return []
    number, words, postcode, qualifier = parts
    splits = [(words, qualifier)] if postcode else [(words[:count], words[count:]) for count in range(len(words), 0, -1)]
    for street, rest in splits:
        if any(re.search(r'[a-z]', word) for word in street):
            found = _find_de((number, street, postcode, rest), geo_directory, limit, near, whole_street=True)
            if found:
                return found
    return []


def _find_de(parts, geo_directory, limit, near, whole_street=False):
    number, words, postcode, qualifier = parts
    street, qualifier = ' '.join(words), ' '.join(qualifier)
    paths = [path for path in sorted((geo_directory / 'addresses').glob('*.sqlite')) if _index_info(path)[0] == DE_NORM]
    if not paths:
        return []
    if whole_street:
        conditions, parameters = ['num IN (?, ?)', 'norm = ?'], [number, number.lower(), street]
    else:
        conditions = ['num IN (?, ?)', 'norm >= ?', 'norm < ?']
        parameters = [number, number.lower(), street, street + '\uffff']
    if postcode:
        conditions.append('zip = ?')
        parameters.append(postcode)
    sql = ('SELECT num,street,city,state,zip,lat,lon,norm,district FROM addr WHERE '
           + ' AND '.join(conditions) + ' ORDER BY length(norm),norm LIMIT 500')
    rows = []
    for path in paths:
        source = _index_info(path)[1] or 'Overture Maps'
        try:
            connection = sqlite3.connect(ro_uri(path), uri=True)
        except sqlite3.Error:
            continue
        try:
            found = connection.execute(sql, parameters).fetchall()
        except sqlite3.Error:
            continue
        finally:
            connection.close()
        rows.extend((*row, source) for row in found if not qualifier or _place_matches(qualifier, row[2], row[8]))
    results, seen = [], set()
    for num, name, city, state, zip_code, lat, lon, normalized, district, source in sorted(rows, key=_rank(near)):
        key = (num, normalized, lat, lon)
        if key in seen:
            continue
        seen.add(key)
        place = f'{city}-{district}' if district else city
        results.append({
            'name': f'{name} {num}', 'kind': 'Street address',
            'detail': ' '.join(part for part in (zip_code, place) if part),
            'lat': lat, 'lon': lon, 'source': source, 'onMap': True,
        })
        if len(results) == limit:
            break
    return results


def _find_us(query, geo_directory, limit, near):
    for reading in _readings(query):
        found = _lookup(*reading, geo_directory, limit, near)
        if found:
            return found
    return []


def _lookup(number, words, state, postcode, plain, town, geo_directory, limit, near):
    if not words:
        return []
    paths = [path for path in sorted((geo_directory / 'addresses').glob('*.sqlite')) if _index_info(path)[0] == US_NORM]
    if state:
        paths = [path for path in paths if path.stem.upper() == state]
    legacy = geo_directory / 'addresses.sqlite'
    if not paths and legacy.is_file():
        paths = [legacy]
    connections = []
    try:
        for path in paths:
            try:
                connections.append(sqlite3.connect(ro_uri(path), uri=True))
            except sqlite3.Error:
                continue
        for count in range(len(words), 0 if town else len(words) - 1, -1):
            street = ' '.join(words[:count])
            if all(word in set(WORDS.values()) for word in words[:count]):
                continue
            city = ' '.join(words[count:])
            rows = []
            for form in street_forms(street):
                conditions = ['num IN (?, ?)', 'norm >= ?', 'norm < ?']
                parameters = [number, number.lower(), form, form + '\uffff']
                if state:
                    conditions.append('state = ?')
                    parameters.append(state)
                if postcode:
                    conditions.append('zip = ?')
                    parameters.append(postcode)
                if city:
                    # normalize() keeps a word's first letter (north->n, saint->st, mount->mt) and only strips
                    # punctuation, so this cheap SQL prefilter never drops a row the Python city check would keep.
                    conditions.append('lower(substr(city,1,1)) = ?')
                    parameters.append(city[0])
                # The city is compared in Python, after normalize() on both sides: the query's "north", "saint" and
                # punctuation must meet the stored "North Hollywood", "Saint Paul" and "St. Louis" in the same form.
                sql = ('SELECT num,street,city,state,zip,lat,lon,norm FROM addr WHERE '
                       + ' AND '.join(conditions) + ' ORDER BY length(norm),norm')
                if not city:
                    sql += ' LIMIT ?'
                    parameters.append(limit * 3)
                plain_city = ' '.join(plain[count:])   # normalize() maps word for word, so the split lines up
                match = str.__eq__ if town == 'whole' else str.startswith   # 'whole': a state's name read as a town of just that name
                for connection in connections:
                    # Rows the city check drops must not use up the limit, so read on until enough pass it.
                    kept = 0
                    try:
                        for row in connection.execute(sql, parameters):
                            stored = row[2] or ''
                            if city and not (match(normalize(stored), city) or match(stored.lower(), plain_city)):
                                continue
                            rows.append(row)
                            kept += 1
                            if kept == limit * 3:
                                break
                    except sqlite3.Error:
                        continue
            if rows:
                results, seen = [], set()
                for num, name, city_name, state_code, zip_code, lat, lon, normalized in sorted(rows, key=_rank(near)):
                    key = (num, normalized, lat, lon)
                    if key in seen:
                        continue
                    seen.add(key)
                    results.append({
                        'name': f'{num} {name}', 'kind': 'Street address',
                        'detail': ', '.join(part for part in (city_name, state_code, zip_code) if part),
                        'lat': lat, 'lon': lon, 'source': 'Overture Maps', 'onMap': True,
                    })
                    if len(results) == limit:
                        break
                return results
        return []
    finally:
        for connection in connections:
            connection.close()
