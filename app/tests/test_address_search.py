"""Offline address lookup must preserve house numbers and geographic qualifiers."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.address_search import (find_addresses, is_address_query, is_german_address_query, is_german_postcode_query,
                                   normalize, normalize_de)


class CountingConnection:
    """Wraps a sqlite3 connection and records how many rows each query's cursor hands back to Python."""

    def __init__(self, connection, fetched):
        self.connection = connection
        self.fetched = fetched

    def execute(self, sql, parameters=()):
        self.fetched.append(0)
        for row in self.connection.execute(sql, parameters):
            self.fetched[-1] += 1
            yield row

    def close(self):
        self.connection.close()


class AddressSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.geo = Path(self.temp.name)
        (self.geo / 'addresses').mkdir()
        self.write_state('MI', [
            ('18001', 'W Nine Mile Road', 'Novi', 'MI', '48375', 42.45, -83.47),
            ('18002', 'W Nine Mile Road', 'Novi', 'MI', '48375', 42.46, -83.47),
            ('18001', 'Nine Mile Rd', 'Farmington', 'MI', '48335', 42.47, -83.40),
        ])
        self.write_state('CA', [
            ('18001', 'Nine Mile Road', 'Example', 'CA', '90001', 34.0, -118.0),
            ('23001', 'Maple Avenue', 'Kansas City', 'MO', '64105', 39.0997, -94.5786),
            ('21', 'W 9 ST', 'Example', 'CA', '90001', 34.01, -118.01),
            ('12', 'Oak Court', 'Example', 'CA', '90001', 34.02, -118.02),
            ('100', 'Main Street', 'Inglewood', 'CA', '90301', 33.96, -118.35),
            ('100', 'Main Street', 'Newport Beach', 'CA', '92661', 33.60, -117.90),
            ('123', 'Main Street', 'North Hollywood', 'CA', '91601', 34.16, -118.37),
            ('123', 'North Main Street', 'North Hollywood', 'CA', '91601', 34.17, -118.38),
        ])
        self.write_state('MN', [('10', 'Oak Avenue', 'Saint Paul', 'MN', '55101', 44.95, -93.09)])
        self.write_state('MO', [('10', 'Oak Avenue', 'St. Louis', 'MO', '63101', 38.63, -90.20)])

    def tearDown(self):
        self.temp.cleanup()

    def write_state(self, state, rows):
        db = sqlite3.connect(self.geo / 'addresses' / f'{state}.sqlite')
        try:
            db.execute('CREATE TABLE addr(num TEXT,street TEXT,norm TEXT,city TEXT,state TEXT,zip TEXT,lat REAL,lon REAL)')
            db.execute('CREATE INDEX addr_num_norm ON addr(num,norm)')
            db.executemany('INSERT INTO addr VALUES(?,?,?,?,?,?,?,?)',
                           [(num, street, normalize(street), city, st, z, lat, lon)
                            for num, street, city, st, z, lat, lon in rows])
            db.commit()
        finally:
            db.close()      # the `with` form commits but does not close, and Windows will not remove an open file

    def test_numeric_words_directions_and_qualifiers(self):
        for query in ('18001 9 mile rd Novi MI', '18001 Nine Mile Road Novi Michigan',
                      '18001 nine mile Novi 48375', '18001 W Nine Mile Road, Novi, MI 48375'):
            with self.subTest(query=query):
                result = find_addresses(query, self.geo)
                self.assertEqual(len(result), 1)
                self.assertEqual(result[0]['name'], '18001 W Nine Mile Road')
                self.assertEqual(result[0]['lat'], 42.45)
                self.assertEqual(result[0]['kind'], 'Street address')

    def test_unknown_number_does_not_return_nearby_house(self):
        self.assertEqual(find_addresses('18003 nine mile Novi MI', self.geo), [])

    def test_city_and_zip_do_not_silently_fall_back(self):
        self.assertEqual(find_addresses('18001 nine mile Wrongtown MI', self.geo), [])
        self.assertEqual(find_addresses('18001 nine mile MI 99999', self.geo), [])

    def test_zip_plus_four_uses_its_five_digit_zip(self):
        for query in ('18001 nine mile Novi MI 48375-1234', '18001 nine mile Novi MI 48375 1234'):
            with self.subTest(query=query):
                result = find_addresses(query, self.geo)
                self.assertEqual(len(result), 1)
                self.assertEqual(result[0]['name'], '18001 W Nine Mile Road')
        self.assertEqual(find_addresses('18001 nine mile Novi MI 99999-1234', self.geo), [])

    def test_state_uses_only_that_index(self):
        result = find_addresses('18001 nine mile CA', self.geo)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['detail'], 'Example, CA, 90001')

    def test_street_ending_that_is_also_a_state_code(self):
        # 'Ct' is Court before it is Connecticut, and 'NE' is northeast before it is Nebraska: neither state is installed here.
        result = find_addresses('12 Oak Ct', self.geo)
        self.assertEqual([r['name'] for r in result], ['12 Oak Court'])
        self.write_state('DC', [
            ('500', '4th Street NE', 'Washington', 'DC', '20002', 38.896, -77.000),
            ('1600', 'Pennsylvania Avenue NW', 'Washington', 'DC', '20500', 38.8977, -77.0365),
        ])
        for query in ('500 4th St NE', '500 4th St NE 20002', '500 4th St NE Washington DC'):
            with self.subTest(query=query):
                self.assertEqual([r['name'] for r in find_addresses(query, self.geo)], ['500 4th Street NE'])

    def test_installed_state_is_still_read_after_a_street_ending(self):
        self.write_state('CT', [('12', 'Oak Street', 'Hartford', 'CT', '06103', 41.76, -72.67)])
        self.assertEqual(find_addresses('12 Oak Ct', self.geo)[0]['detail'], 'Example, CA, 90001')      # the Court, as typed
        self.assertEqual(find_addresses('12 Oak St CT', self.geo)[0]['detail'], 'Hartford, CT, 06103')
        self.assertEqual(find_addresses('12 Oak Connecticut', self.geo)[0]['detail'], 'Hartford, CT, 06103')

    def test_town_named_like_a_state(self):
        self.write_state('DC', [('1600', 'Pennsylvania Avenue NW', 'Washington', 'DC', '20500', 38.8977, -77.0365)])
        for query in ('1600 Pennsylvania Ave NW Washington', '1600 Pennsylvania Ave NW Washington DC'):
            with self.subTest(query=query):
                self.assertEqual([r['detail'] for r in find_addresses(query, self.geo)], ['Washington, DC, 20500'])

    def test_state_that_is_not_installed_is_never_read_as_a_town(self):
        # Indiana, Nebraska and Kansas are not installed. 'IN' must not find Inglewood, 'NE' Newport Beach, or 'Kansas' Kansas City.
        for query in ('100 Main St IN', '100 Main St NE', '100 Main St Indiana', '100 Main St IN 90301', '23001 Maple Ave Kansas'):
            with self.subTest(query=query):
                self.assertEqual(find_addresses(query, self.geo), [])

    def test_ordinal_street(self):
        result = find_addresses('21 ninth st Example CA', self.geo)
        self.assertEqual(result[0]['name'], '21 W 9 ST')

    def test_city_with_direction_or_saint_matches_stored_title_case(self):
        result = find_addresses('123 Main St North Hollywood', self.geo)
        self.assertIn('123 Main Street', [row['name'] for row in result])
        result = find_addresses('123 North Main Street North Hollywood CA', self.geo)
        self.assertEqual(result[0]['name'], '123 North Main Street')
        self.assertEqual(find_addresses('123 Main St Hollywood North', self.geo), [])
        for query in ('10 Oak Ave Saint Paul MN', '10 Oak Ave St Paul MN', '10 Oak Ave Saint Paul'):
            with self.subTest(query=query):
                result = find_addresses(query, self.geo)
                self.assertEqual(len(result), 1)
                self.assertEqual(result[0]['name'], '10 Oak Avenue')
                self.assertEqual(result[0]['detail'], 'Saint Paul, MN, 55101')

    def test_city_punctuation_is_ignored(self):
        for query in ('10 Oak Ave St Louis MO', '10 Oak Ave Saint Louis MO', '10 Oak Ave St. Louis MO'):
            with self.subTest(query=query):
                result = find_addresses(query, self.geo)
                self.assertEqual(len(result), 1)
                self.assertEqual(result[0]['detail'], 'St. Louis, MO, 63101')
        self.assertEqual(find_addresses('10 Oak Ave Saint Louis Park MO', self.geo), [])

    def test_diagonal_directions_match_full_and_abbreviated_names(self):
        directions = (('NE', 'Northeast'), ('NW', 'Northwest'), ('SE', 'Southeast'), ('SW', 'Southwest'))
        rows = []
        for number, (short, full) in enumerate(directions, 100):
            rows.extend([
                (str(number), f'{full} 9th Street', 'Example', 'WA', '98000', 0.0, 0.0),
                (str(number + 100), f'9th Street {short}', 'Example', 'WA', '98000', 0.0, 0.0),
                (str(number + 200), f'{short} 9th Street', 'Example', 'WA', '98000', 0.0, 0.0),
                (str(number + 300), f'9th Street {full}', 'Example', 'WA', '98000', 0.0, 0.0),
            ])
        self.write_state('WA', rows)
        for number, (short, full) in enumerate(directions, 100):
            for query, name in (
                    (f'{number} {short} Ninth St WA', f'{number} {full} 9th Street'),
                    (f'{number} {full} Ninth St WA', f'{number} {full} 9th Street'),
                    (f'{number + 100} Ninth St {short} WA', f'{number + 100} 9th Street {short}'),
                    (f'{number + 100} Ninth St {full} WA', f'{number + 100} 9th Street {short}'),
                    (f'{number + 200} {short} Ninth St WA', f'{number + 200} {short} 9th Street'),
                    (f'{number + 200} {full} Ninth St WA', f'{number + 200} {short} 9th Street'),
                    (f'{number + 300} Ninth St {short} WA', f'{number + 300} 9th Street {full}'),
                    (f'{number + 300} Ninth St {full} WA', f'{number + 300} 9th Street {full}')):
                with self.subTest(query=query):
                    result = find_addresses(query, self.geo)
                    self.assertEqual([row['name'] for row in result], [name])
        self.assertEqual(find_addresses('100 NW Ninth St WA', self.geo), [])
        self.assertEqual(find_addresses('101 NE Ninth St WA', self.geo), [])
        self.assertEqual(find_addresses('999 NE Ninth St WA', self.geo), [])
        self.assertEqual(find_addresses('100 NE Ninth St WA 99999', self.geo), [])

    def test_plain_zip_remains_place_query(self):
        self.assertFalse(is_address_query('48375'))
        self.assertFalse(is_address_query('Novi MI'))
        self.assertTrue(is_address_query('18001 nine mile'))

    def test_missing_indexes_and_legacy_database(self):
        empty = self.geo / 'empty'
        self.assertEqual(find_addresses('23001 Maple Ave', empty), [])
        empty.mkdir()
        db = sqlite3.connect(empty / 'addresses.sqlite')
        try:
            db.execute('CREATE TABLE addr(num TEXT,street TEXT,norm TEXT,city TEXT,state TEXT,zip TEXT,lat REAL,lon REAL)')
            db.execute("INSERT INTO addr VALUES('23001','Maple Avenue','maple ave','Kansas City','MO','64105',39.0997,-94.5786)")
            db.commit()
        finally:
            db.close()      # as in write_state: the `with` form does not close, and Windows will not remove an open file
        self.assertEqual(find_addresses('23001 Maple Ave Kansas City MO', empty)[0]['lat'], 39.0997)

    def test_null_city_row_does_not_break_city_query(self):
        self.write_state('OH', [
            ('5', 'Elm Street', None, 'OH', '43001', 40.0, -83.0),
            ('5', 'Elm Street', 'Columbus', 'OH', '43215', 39.96, -83.0),
        ])
        result = find_addresses('5 elm Columbus OH', self.geo)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['detail'], 'Columbus, OH, 43215')

    def test_city_query_fetches_only_rows_it_can_keep(self):
        # Same number and street in other cities: only the first-letter prefilter should keep them out of Python.
        self.write_state('IL', [('100', 'Main Street', 'Springfield', 'IL', '62701', 39.80, -89.64)])
        self.write_state('NV', [('100', 'Main Street', 'Boston', 'NV', '89501', 39.5, -119.8)] * 300)
        fetched = []
        real_connect = sqlite3.connect
        with patch('engine.address_search.sqlite3.connect',
                   side_effect=lambda *args, **kwargs: CountingConnection(real_connect(*args, **kwargs), fetched)):
            result = find_addresses('100 main springfield', self.geo)
        self.assertEqual(result[0]['detail'], 'Springfield, IL, 62701')
        self.assertLessEqual(sum(fetched), 10)

    def test_city_match_is_not_crowded_out_by_same_first_letter_rows(self):
        self.write_state('IL', [('100', 'Main Street', 'Sparks', 'IL', '62702', 39.5, -119.7)] * 160
                         + [('100', 'Main Street', 'Springfield', 'IL', '62701', 39.80, -89.64)])
        result = find_addresses('100 Main Street Springfield IL', self.geo)
        self.assertEqual([row['detail'] for row in result], ['Springfield, IL, 62701'])

    def test_city_query_stops_reading_once_it_has_enough_matches(self):
        self.write_state('IL', [('100', 'Main Street', 'Springfield', 'IL', '62701', 39.80, -89.64)] * 300)
        fetched = []
        real_connect = sqlite3.connect
        with patch('engine.address_search.sqlite3.connect',
                   side_effect=lambda *args, **kwargs: CountingConnection(real_connect(*args, **kwargs), fetched)):
            result = find_addresses('100 main springfield', self.geo)
        self.assertEqual(result[0]['detail'], 'Springfield, IL, 62701')
        self.assertLessEqual(max(fetched), 8 * 3)

    def test_partial_city_prefix_matches_direction_and_saint_cities(self):
        result = find_addresses('123 Main St Nort CA', self.geo)
        self.assertEqual(result[0]['detail'], 'North Hollywood, CA, 91601')
        result = find_addresses('10 Oak Ave Sain MN', self.geo)
        self.assertEqual(result[0]['detail'], 'Saint Paul, MN, 55101')

    def test_app_route_preserves_address_number(self):
        import server
        with patch.object(server, 'HERE', self.geo.parent), patch.object(server.address_search, 'find_addresses', return_value=[]) as lookup:
            self.assertEqual(server.places('18003 nine mile'), [])
            lookup.assert_called_once_with('18003 nine mile', self.geo.parent / 'derived' / 'geo', limit=8, near=None)


class QuebecAddressTests(unittest.TestCase):
    """Invented Quebec addresses (issue #169). Every street, number and postal code here is made up; H0H 0H0 is Canada
    Post's well-known placeholder. Cases marked expectedFailure describe wanted behaviour the US parser does not have yet:
    when a change makes one pass, remove its marker."""

    ROWS = [
        ('1250', 'rue des Érables', 'Saint-Exemple', 'QC', 'H0H0H0', 46.10, -73.10),
        ('88', 'boulevard de la Rivière', 'Saint-Exemple', 'QC', 'H0H0H0', 46.11, -73.11),
        ('45', 'rue Sainte-Thérèse', 'Saint-Exemple', 'QC', 'H0H0H0', 46.12, -73.12),
        ('7', 'rue Saint-Exemple', 'Exempleville', 'QC', 'H0H0H0', 46.13, -73.13),
        ('500', 'Exemple Street', 'Exempleville', 'QC', 'H0H0H0', 46.14, -73.14),
        ('15', 'rue du Cœur-Vaillant', 'Exempleville', 'QC', 'H0H0H0', 46.15, -73.15),
        ('300', 'rang du Lac-Vert', 'Exempleville', 'QC', 'H0H0H0', 46.16, -73.16),
        ('9', "chemin d'Exemple", 'Exempleville', 'QC', 'H0H0H0', 46.17, -73.17),
        ('31', 'rue François', 'Exempleville', 'QC', 'H0H0H0', 46.18, -73.18),
    ]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.geo = Path(self.temp.name)
        (self.geo / 'addresses').mkdir()
        db = sqlite3.connect(self.geo / 'addresses' / 'QC.sqlite')
        try:
            db.execute('CREATE TABLE addr(num TEXT,street TEXT,norm TEXT,city TEXT,state TEXT,zip TEXT,lat REAL,lon REAL)')
            db.execute('CREATE INDEX addr_num_norm ON addr(num,norm)')
            db.executemany('INSERT INTO addr VALUES(?,?,?,?,?,?,?,?)',
                           [(num, street, normalize(street), city, st, z, lat, lon)
                            for num, street, city, st, z, lat, lon in self.ROWS])
            db.commit()
        finally:
            db.close()

    def tearDown(self):
        self.temp.cleanup()

    def assertFinds(self, query, name):
        result = find_addresses(query, self.geo)
        self.assertTrue(result, f'{query!r} found nothing')
        self.assertEqual(result[0]['name'], name)

    # Works today -----------------------------------------------------------------------------------------------------

    def test_same_accents_on_both_sides(self):
        # Works because the query and the index lose the accented letters the same way, not because accents are handled.
        self.assertFinds('1250 rue des Érables', '1250 rue des Érables')
        self.assertFinds('88 boulevard de la Rivière', '88 boulevard de la Rivière')

    def test_hyphen_dash_and_apostrophe_forms(self):
        for query in ('300 rang du Lac-Vert', '300 rang du Lac Vert', '300 rang du Lac\u2013Vert', '300 rang du Lac\u2010Vert'):
            with self.subTest(query=query):
                self.assertFinds(query, '300 rang du Lac-Vert')
        for query in ("9 chemin d'Exemple", '9 chemin d\u2019Exemple', '9 chemin d Exemple'):
            with self.subTest(query=query):
                self.assertFinds(query, "9 chemin d'Exemple")

    def test_saint_before_a_name(self):
        for query in ('7 rue St-Exemple', '7 rue Saint-Exemple', '7 rue st exemple'):
            with self.subTest(query=query):
                self.assertFinds(query, '7 rue Saint-Exemple')

    def test_street_after_a_name(self):
        for query in ('500 Exemple Street', '500 Exemple St.', '500 Exemple St'):
            with self.subTest(query=query):
                self.assertFinds(query, '500 Exemple Street')

    def test_saint_and_street_do_not_cross(self):
        # "St" before a name is Saint, after it Street: neither form finds the other street.
        self.assertEqual(find_addresses('500 rue St-Exemple', self.geo), [])
        self.assertEqual(find_addresses('7 Exemple St', self.geo), [])

    # Wanted, not supported yet -------------------------------------------------------------------------------------

    @unittest.expectedFailure
    def test_query_without_accents_finds_accented_street(self):
        # Today "Érables" is stored as "rables", so a query typed without accents ("erables") misses it.
        self.assertFinds('1250 rue des Erables', '1250 rue des Érables')

    @unittest.expectedFailure
    def test_cedilla_folds_to_c(self):
        self.assertFinds('31 rue Francois', '31 rue François')

    @unittest.expectedFailure
    def test_ligature_oe_folds_to_oe(self):
        # NFKD does not decompose œ, so it needs its own rule.
        self.assertFinds('15 rue du Coeur-Vaillant', '15 rue du Cœur-Vaillant')

    @unittest.expectedFailure
    def test_sainte_and_ste_are_the_same(self):
        self.assertFinds('45 rue Ste-Thérèse', '45 rue Sainte-Thérèse')

    @unittest.expectedFailure
    def test_canadian_postal_code_with_or_without_space(self):
        for query in ('1250 rue des Érables H0H 0H0', '1250 rue des Érables h0h0h0'):
            with self.subTest(query=query):
                self.assertFinds(query, '1250 rue des Érables')

    @unittest.expectedFailure
    def test_unit_before_number(self):
        # In Canada "3-1250" is unit 3 at house number 1250.
        self.assertFinds('3-1250 rue des Érables', '1250 rue des Érables')


class OntarioAddressTests(unittest.TestCase):
    """Invented Ontario addresses, as written by people in Ontario.

    Every street name and house number is made up, and so is the postal code. Exampleton is an invented town; Sault
    Ste. Marie and St. Catharines are real cities, used only for how their names are written, with invented streets.

    The index follows the layout proposed in #143: its own `CA-ON.sqlite`, `CA-ON` as the region and postal codes
    stored without the space. Main has no Ontario index yet, so the format is a guess; the cases are the point. A US
    index with the same street is installed too, so a case that finds the Ohio row instead is caught.

    Cases marked `expectedFailure` are wanted but fail today by finding nothing, not by an error. Remove the marker
    when a change makes one pass.
    """

    POSTAL = 'M0M 0M0'      # made up

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.geo = Path(self.temp.name)
        (self.geo / 'addresses').mkdir()
        self.write_index('CA-ON', [
            ('120', 'Exemplar Street East', 'Exampleton', 'CA-ON', self.POSTAL.replace(' ', ''), 44.10, -79.10),
            ('4410', 'Highway 7', 'Exampleton', 'CA-ON', '', 44.11, -79.11),
            ('5521', '10th Line', 'Exampleton', 'CA-ON', '', 44.12, -79.12),
            ('3100', 'Concession Road 6', 'Exampleton', 'CA-ON', '', 44.13, -79.13),
            ('2210', 'County Road 2', 'Exampleton', 'CA-ON', '', 44.14, -79.14),
            ('45', 'Exemplar Road', 'Sault Ste. Marie', 'CA-ON', '', 46.50, -84.30),
            ('46', 'Exemplar Road', 'St. Catharines', 'CA-ON', '', 43.15, -79.24),
        ])
        self.write_index('OH', [
            ('120', 'Exemplar Street East', 'Exampleton', 'OH', '44000', 40.00, -82.00),
        ])

    def tearDown(self):
        self.temp.cleanup()

    def write_index(self, name, rows):
        db = sqlite3.connect(self.geo / 'addresses' / f'{name}.sqlite')
        try:
            db.execute('CREATE TABLE addr(num TEXT,street TEXT,norm TEXT,city TEXT,state TEXT,zip TEXT,lat REAL,lon REAL)')
            db.execute('CREATE INDEX addr_num_norm ON addr(num,norm)')
            db.executemany('INSERT INTO addr VALUES(?,?,?,?,?,?,?,?)',
                           [(num, street, normalize(street), city, region, code, lat, lon)
                            for num, street, city, region, code, lat, lon in rows])
            db.commit()
        finally:
            db.close()      # as in AddressSearchTests.write_state

    def assertFinds(self, query, name, lat):
        result = find_addresses(query, self.geo)
        self.assertTrue(result, f'{query!r} found nothing')
        self.assertEqual(result[0]['name'], name)
        self.assertEqual(result[0]['lat'], lat)

    def assertOntarioOnly(self, query):
        result = find_addresses(query, self.geo)
        self.assertEqual([r['lat'] for r in result], [44.10], f'{query!r}: {result}')

    # Pass today: regression guards.

    def test_direction_after_the_street(self):
        """Ontario writes the direction after the street ("King St E"), not before it as in "W Nine Mile Road"."""
        for query in ('120 Exemplar Street East', '120 Exemplar St E', '120 exemplar st. e.', '120 Exemplar St E Exampleton'):
            with self.subTest(query=query):
                self.assertFinds(query, '120 Exemplar Street East', 44.10)

    def test_highway_and_county_road(self):
        for query in ('4410 Highway 7', '4410 Hwy 7', '4410 Hwy. 7'):
            with self.subTest(query=query):
                self.assertFinds(query, '4410 Highway 7', 44.11)
        for query in ('2210 County Road 2', '2210 County Rd 2'):
            with self.subTest(query=query):
                self.assertFinds(query, '2210 County Road 2', 44.14)

    def test_numbered_line(self):
        """Rural townships number their roads: "10th Line", also written "Tenth Line"."""
        for query in ('5521 10th Line', '5521 Tenth Line'):
            with self.subTest(query=query):
                self.assertFinds(query, '5521 10th Line', 44.12)

    def test_concession_road(self):
        for query in ('3100 Concession Road 6', '3100 Concession Rd 6'):
            with self.subTest(query=query):
                self.assertFinds(query, '3100 Concession Road 6', 44.13)

    # Wanted: fail today.

    @unittest.expectedFailure
    def test_province_abbreviation(self):
        """"ON" is not read as a province, so it stays in the city: the city "exampleton on" matches nothing."""
        self.assertOntarioOnly('120 Exemplar St E Exampleton ON')

    @unittest.expectedFailure
    def test_province_name(self):
        self.assertOntarioOnly('120 Exemplar St E Exampleton Ontario')

    @unittest.expectedFailure
    def test_postal_code_with_space(self):
        """A postal code is letter-digit-letter digit-letter-digit; only a five-digit ZIP is read as a code today."""
        self.assertOntarioOnly(f'120 Exemplar St E, Exampleton, ON {self.POSTAL}')

    @unittest.expectedFailure
    def test_postal_code_without_space_or_capitals(self):
        for code in (self.POSTAL.replace(' ', ''), self.POSTAL.lower()):
            with self.subTest(code=code):
                self.assertOntarioOnly(f'120 Exemplar St E {code}')

    @unittest.expectedFailure
    def test_unit_before_the_house_number(self):
        """Canada writes unit 4 at number 120 as "4-120"; today "4-120" is read as the house number."""
        self.assertFinds('4-120 Exemplar St E', '120 Exemplar Street East', 44.10)

    def test_city_with_ste(self):
        """The query's city is normalized ("ste marie"), the stored one is not ("ste. marie"): both are compared normalized."""
        for query in ('45 Exemplar Rd Sault Ste Marie', '45 Exemplar Rd Sault Ste. Marie'):
            with self.subTest(query=query):
                self.assertFinds(query, '45 Exemplar Road', 46.50)

    def test_city_with_st(self):
        for query in ('46 Exemplar Rd St Catharines', '46 Exemplar Rd St. Catharines', '46 Exemplar Rd Saint Catharines'):
            with self.subTest(query=query):
                self.assertFinds(query, '46 Exemplar Road', 43.15)


FIXTURE = json.loads((Path(__file__).resolve().parent / 'fixtures' / 'de_be_queries.json').read_text(encoding='utf-8'))
BERLIN = FIXTURE['address_rows']        # the German query cases shared with the builder (tools/build_region_de_be.py)


class GermanAddressSearchTests(unittest.TestCase):
    """Berlin addresses are written street first, number after, and may carry a postcode and an Ortsteil."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.geo = Path(self.temp.name)
        (self.geo / 'addresses').mkdir()
        self.write_berlin(self.geo / 'addresses' / FIXTURE['index_filename'])

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def write_berlin(path, rows=BERLIN):
        """The DE-BE index contract from the fixture: a district column and meta norm 'de-1' with its own source line."""
        db = sqlite3.connect(path)
        try:
            db.execute('CREATE TABLE addr(num TEXT,street TEXT,norm TEXT,city TEXT,state TEXT,zip TEXT,lat REAL,lon REAL,district TEXT)')
            db.execute('CREATE INDEX addr_norm ON addr(norm,num)')
            db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)')
            db.executemany('INSERT INTO addr VALUES(?,?,?,?,?,?,?,?,?)',
                           [(row['num'], row['street'], row['norm'], row['city'], row['state'], row['zip'], row['lat'], row['lon'],
                             row['district']) for row in rows])
            db.executemany('INSERT INTO meta VALUES(?,?)', [('region', FIXTURE['region']), ('norm', FIXTURE['norm']),
                                                            ('source', rows[0]['source'])])
            db.commit()
        finally:
            db.close()      # as in AddressSearchTests.write_state: Windows will not remove an open file

    def write_us_state(self, state, rows, source=None):
        AddressSearchTests.write_state(self, state, rows)
        if source:      # the published US state indexes carry a source line but no norm key
            db = sqlite3.connect(self.geo / 'addresses' / f'{state}.sqlite')
            try:
                db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)')
                db.execute("INSERT INTO meta VALUES('source',?)", (source,))
                db.commit()
            finally:
                db.close()

    def one(self, query):
        result = find_addresses(query, self.geo)
        self.assertEqual(len(result), 1, query)
        return result[0]

    def test_fixture_queries_find_exactly_the_expected_houses(self):
        """Every German query in the shared fixture: street first, umlaut and 'Str.' spellings, a suffix letter, a number
        inside the street name, an optional postcode, city or Ortsteil. Each finds exactly its expected houses, named
        the German way and credited to the index's own source; an unknown number or a wrong place finds nothing."""
        rows = {row['id']: row for row in BERLIN}
        for case in FIXTURE['address_queries'] + FIXTURE['no_house_queries']:
            with self.subTest(query=case['query']):
                found = find_addresses(case['query'], self.geo)
                expected = [rows[key] for key in case['expected_fixture_ids']]
                self.assertCountEqual([(hit['lat'], hit['lon']) for hit in found], [(row['lat'], row['lon']) for row in expected])
                for hit in found:
                    row = next(row for row in expected if (row['lat'], row['lon']) == (hit['lat'], hit['lon']))
                    self.assertEqual(hit['name'], f"{row['street']} {row['num']}")
                    self.assertEqual(hit['detail'], f"{row['zip']} {row['city']}-{row['district']}")
                    self.assertEqual((hit['kind'], hit['source']), ('Street address', row['source']))   # not Overture
                    if 'expected_name' in case:
                        self.assertEqual(hit['name'], case['expected_name'])
                    if 'expected_detail' in case:
                        self.assertEqual(hit['detail'], case['expected_detail'])

    def test_normalize_de_scheme(self):
        """Indexes store normalize_de(street) in their norm column. The builder keeps its own copy so the data half needs
        nothing from engine/; both must give the stored key, or a house would be in the index and never found."""
        from tools.build_region_de_be import normalize_de as builder_normalize_de
        for example in FIXTURE['normalization_examples']:
            with self.subTest(text=example['input']):
                self.assertEqual(normalize_de(example['input']), example['expected'])
        for text in [row['street'] for row in BERLIN] + [example['input'] for example in FIXTURE['normalization_examples']] + [
                'Karl-Marx-Str.', 'Hauptstrasse', 'Müllerstraße', 'Ärztehaus Göhrener Straße', '']:
            with self.subTest(text=text):
                self.assertEqual(normalize_de(text), builder_normalize_de(text))
        self.assertEqual(normalize_de('Straße des 17. Juni'), 'str des 17 juni')
        self.assertEqual(normalize('18001 W Nine Mile Road'), '18001 w nine mile rd')      # the US scheme is untouched

    def test_fixture_place_queries_are_not_addresses(self):
        """'10117 Berlin', 'Kreuzberg', 'Straße des 17. Juni' or 'Route 66' name places: no address may answer them."""
        for case in FIXTURE['place_queries']:
            with self.subTest(query=case['query']):
                self.assertTrue(case['must_not_be_address'])
                self.assertEqual(find_addresses(case['query'], self.geo), [])

    def test_postcode_city_and_district_alone_stay_place_queries(self):
        """'10117 Berlin', 'Berlin' and 'Kreuzberg' name places; treating them as addresses would hide the place."""
        for query in ('10117 Berlin', 'Berlin', 'Kreuzberg', '10117'):
            with self.subTest(query=query):
                self.assertFalse(is_german_address_query(query))
                self.assertEqual(find_addresses(query, self.geo), [])
        # '10117 Berlin' is number-first like '18001 nine mile', so places() asks whether a German index knows it
        self.assertTrue(is_address_query('10117 Berlin'))
        self.assertTrue(is_german_postcode_query('10117 Berlin', self.geo))
        self.assertTrue(is_german_postcode_query('10999, Kreuzberg', self.geo))
        self.assertFalse(is_german_postcode_query('18003 nine mile', self.geo))
        self.assertFalse(is_german_postcode_query('10117 Spandau', self.geo))
        self.assertTrue(is_german_address_query('Unter den Linden 77'))

    def test_us_results_unchanged_with_berlin_installed(self):
        """Adding DE-BE must not change a single US answer, including the 'Overture Maps' credit."""
        (self.geo / 'addresses' / 'DE-BE.sqlite').unlink()
        self.write_us_state('MI', [('18001', 'W Nine Mile Road', 'Novi', 'MI', '48375', 42.45, -83.47),
                                   ('77', 'Linden Street', 'Novi', 'MI', '48375', 42.40, -83.40)],
                            source='Overture Maps addresses 2026-10-21.0 (open licences, mostly CDLA-Permissive-2.0)')
        self.write_us_state('CA', [('21', 'W 9 ST', 'Example', 'CA', '90001', 34.01, -118.01)])
        queries = ('18001 nine mile', '18001 W Nine Mile Road, Novi, MI 48375', '77 Linden', '77 Linden St Novi',
                   '21 ninth st Example CA', '18003 nine mile')
        before = [find_addresses(query, self.geo) for query in queries]
        self.assertEqual(before[0][0]['source'], 'Overture Maps')
        self.write_berlin(self.geo / 'addresses' / 'DE-BE.sqlite')
        self.assertEqual([find_addresses(query, self.geo) for query in queries], before)

    def test_number_first_finds_a_german_house_that_no_us_index_has(self):
        """The search box asks for a US-style address, and the Berlin map on main since 1.1.17 found '77 Unter den
        Linden' by accident. The German index answers it on purpose once no US index does, but only for a whole stored
        street: '77 Linden' must not become Lindenstraße or Unter den Linden, and a wrong number or place finds nothing."""
        rows = {row['id']: row for row in BERLIN}
        cases = {
            '77 Unter den Linden': ['linden-77'], '77 Unter den Linden, 10117 Berlin': ['linden-77'],
            '77 unter den linden berlin': ['linden-77'], '12a Müllerstraße': ['mueller-12a'], '12A Muellerstr.': ['mueller-12a'],
            '135 Straße des 17. Juni': ['juni-135'], '25 Oranienstr Kreuzberg': ['oranien-25'],
            '5 Hauptstraße': ['haupt-5-schoeneberg', 'haupt-5-rahnsdorf'], '5 Hauptstraße Rahnsdorf': ['haupt-5-rahnsdorf'],
            '78 Unter den Linden': [], '12b Müllerstraße': [], '77 Linden': [], '77 Unter den': [], '77 Lindenstr Mitte': [],
            '25 Oranienstraße Spandau': [], '25 Oranienstraße, 10117 Berlin': [], '10117 Berlin': [],
        }
        for query, ids in cases.items():
            with self.subTest(query=query):
                found = find_addresses(query, self.geo)
                self.assertCountEqual([(hit['lat'], hit['lon']) for hit in found], [(rows[i]['lat'], rows[i]['lon']) for i in ids])
                for hit in found:
                    self.assertEqual((hit['kind'], hit['source']), ('Street address', BERLIN[0]['source']))
        self.assertEqual(self.one('77 Unter den Linden')['name'], 'Unter den Linden 77')    # named the German way

    def test_a_us_house_still_wins_a_number_first_query(self):
        """The German index is asked only when every US index has said no: a US house answers alone, as before."""
        self.write_us_state('PA', [('5', 'Hauptstrasse', 'Example', 'PA', '19001', 40.1, -75.1)])
        hit = self.one('5 Hauptstrasse')
        self.assertEqual((hit['detail'], hit['source']), ('Example, PA, 19001', 'Overture Maps'))

    def test_delaware_stays_delaware(self):
        """Bare 'DE' is the US state code: a Delaware query must use DE.sqlite only, never the DE-BE index."""
        self.write_us_state('DE', [('77', 'Linden Street', 'Wilmington', 'DE', '19801', 39.74, -75.55)])
        for query in ('77 Linden DE', '77 Linden Delaware'):
            with self.subTest(query=query):
                hit = self.one(query)
                self.assertEqual(hit['detail'], 'Wilmington, DE, 19801')
                self.assertEqual(hit['source'], 'Overture Maps')

    def test_app_route_falls_back_to_places_for_route_66(self):
        """'Route 66' has the German shape and '10117 Berlin' the US one, but both are places: with no such house the
        normal place search answers. An unknown US house stays an empty address answer, as before."""
        import server
        root = self.geo / 'root'
        geo = root / 'derived' / 'geo'
        (geo / 'addresses').mkdir(parents=True)
        self.write_berlin(geo / 'addresses' / 'DE-BE.sqlite')
        places = sqlite3.connect(geo / 'places.sqlite')
        try:
            places.executescript(
                "CREATE TABLE place(id INTEGER PRIMARY KEY, name TEXT, kind TEXT, detail TEXT, lat REAL, lon REAL, rank INTEGER, source TEXT);"
                "CREATE VIRTUAL TABLE place_fts USING fts5(name, detail, content='place', content_rowid='id', tokenize='unicode61');"
                "INSERT INTO place(name,kind,detail,lat,lon,rank,source) VALUES ('Route 66','Locale','Arizona',35.2,-113.9,3,'gnis');"
                "INSERT INTO place(name,kind,detail,lat,lon,rank,source) VALUES ('Berlin','City','Germany',52.52,13.40,1,'test');"
                "INSERT INTO place(name,kind,detail,lat,lon,rank,source) VALUES ('Nine Mile','Locale','Michigan',42.4,-83.4,3,'gnis');"
                "INSERT INTO place_fts(place_fts) VALUES('rebuild');")
            places.commit()
        finally:
            places.close()
        with patch.object(server, 'HERE', root), patch.object(server, 'PLACEDB', geo / 'places.sqlite'), \
                patch.object(server, 'WATERDB', root / 'no-water.sqlite'), patch.object(server, 'home_area', return_value=None):
            self.assertEqual([hit['name'] for hit in server.places('Route 66')], ['Route 66'])
            self.assertEqual([hit['name'] for hit in server.places('10117 Berlin')], ['Berlin'])
            self.assertEqual([hit['name'] for hit in server.places('Kreuzberg')], [])
            self.assertEqual(server.places('18003 nine mile'), [])
            hit, = server.places('Unter den Linden 77')
            self.assertEqual((hit['name'], hit['kind']), ('Unter den Linden 77', 'Street address'))


if __name__ == '__main__':
    unittest.main()
