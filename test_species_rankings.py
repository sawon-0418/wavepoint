"""python3 -m unittest test_species_rankings

랭킹 SELECT는 SQLite의 윈도 함수로 검증한다.
PostgreSQL 트리거·권한·RPC는 실제 Supabase 적용 후 별도 확인이 필요하다.
"""
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch
import server


class RankingTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.execute("attach database ':memory:' as public")
        self.db.execute('create table public.posts (id text, spot_id text, author_id text, author text, species_id text, species text, length real, length_is_ai boolean, created_at text, is_hidden boolean)')
        self.db.execute('create table public.community_spots (id text, is_hidden boolean)')
        sql = Path('supabase_schema.sql').read_text()
        self.query = sql.split('with personal_best as (', 1)[1].split(';', 1)[0]
        self.query = 'with personal_best as (' + self.query.replace('s.id::text', 'cast(s.id as text)')

    def tearDown(self):
        self.db.close()

    def add(self, ident, owner, length, species='bass', hidden=False, spot='visible'):
        self.db.execute('insert into public.posts values (?,?,?,?,?,?,?,?,?,?)', (ident, spot, owner, owner, species, species, length, False, '2026-09-22', hidden))

    def rows(self):
        return [dict(row) for row in self.db.execute(self.query)]

    def test_personal_best_and_independent_species_and_ties(self):
        self.add('a', 'one', 40)
        self.add('b', 'one', 60)
        self.add('c', 'two', 60)
        self.add('d', 'three', 50)
        self.add('e', 'one', 20, 'rockfish')
        rows = {row['id']: row for row in self.rows()}
        self.assertNotIn('a', rows)
        self.assertEqual([rows[key]['rank'] for key in ('b','c','d','e')], [1,1,3,1])

    def test_hidden_spot_and_post_excluded_and_edit_delete_reflected(self):
        self.db.execute("insert into public.community_spots values ('hidden', true)")
        self.add('a','one',100,hidden=True)
        self.add('b','two',90,spot='hidden')
        self.add('c','three',40)
        self.add('d','four',30)
        self.assertEqual(len(self.rows()), 2)
        self.db.execute("update public.posts set length=50, species_id='rockfish' where id='d'")
        self.assertTrue(all(row['rank'] == 1 for row in self.rows()))
        self.db.execute("delete from public.posts where id='d'")
        self.assertEqual([row['id'] for row in self.rows()], ['c'])

    def test_ranking_not_truncated_at_100(self):
        for index in range(150): self.add(str(index), str(index), 200-index)
        rows = {row['id']: row for row in self.rows()}
        self.assertEqual(rows['149']['rank'], 150)

    def test_length_validation(self):
        for value in ('nan', 'inf', -1, 301, True, 'abc'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                server.catch_fields({'species':'우럭','length':value})
        with self.assertRaises(ValueError): server.catch_fields({'species':'  ','length':20})
        self.assertEqual(server.catch_fields({'species':' 우럭 ','length':'25.5'}), {'species':'우럭','length':25.5})
        self.assertIsNone(server.catch_fields({'species':'','length':None})['length'])

    def test_api_uses_session_identity(self):
        handler = object.__new__(server.Handler)
        handler.path = '/api/species-rankings?viewerId=someone-else'
        handler.session_user = lambda: {'id':'actual-user'}
        handler.send_json = lambda body, status=200: (body, status)
        with patch.object(server, 'supabase_request', return_value={'species':[], 'top':[], 'myRank':None}) as rpc:
            body, status = handler.do_GET()
            self.assertEqual(status, 200)
            self.assertEqual(rpc.call_args.args[2]['p_viewer_id'], 'actual-user')
            self.assertEqual(body['viewerId'], 'actual-user')
        handler.path = '/api/species-rankings?speciesId=invalid'
        with patch.object(server, 'supabase_request') as rpc:
            self.assertEqual(handler.do_GET()[1], 400)
            rpc.assert_not_called()


if __name__ == '__main__': unittest.main()
