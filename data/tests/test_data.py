"""
Data (P4): SQL over the user's databases, and one generic API caller.

Pinned: reads are parsed (never regex) and run read-only; writes, DDL and
chained statements are refused with the reason; hosts go through the egress
guard; scopes say which connections are in play (empty means none, absent
means any the user owns); big results land as a CSV, never as a turn-killing
payload; auth comes from the vault, never from the model.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings

from chat.tools import execute_tool
from data import drivers
from data.models import ApiConnection, DataConnection
from data.sqlcheck import SqlRefused, check
from inference import vfs
from inference.models import Document

User = get_user_model()


class SqlCheckTests(TestCase):
    def test_reads_pass(self):
        for sql in ('SELECT * FROM orders', 'WITH x AS (SELECT 1) SELECT * FROM x',
                    'VALUES (1, 2)', 'EXPLAIN SELECT 1'):
            self.assertTrue(check(sql))

    def test_writes_ddl_and_chains_are_refused(self):
        for sql in ('DELETE FROM orders', 'DROP TABLE orders',
                    'SELECT 1; DELETE FROM orders', 'COPY orders TO STDOUT',
                    'SELECT pg_read_file(\'/etc/passwd\')'):
            with self.assertRaises(SqlRefused, msg=sql):
                check(sql)

    def test_writes_pass_only_through_the_write_door(self):
        self.assertTrue(check('UPDATE orders SET n = 1', write=True))
        with self.assertRaises(SqlRefused):
            check('DROP TABLE orders', write=True)


class SqliteToolTests(TestCase):
    def setUp(self):
        self._media = tempfile.mkdtemp()
        self._override = override_settings(MEDIA_ROOT=self._media)
        self._override.enable()
        self.user = User.objects.create_user('analyst', 'a@example.com', 'pw')
        self.scope = vfs.chat_scope(self.user)
        path = tempfile.mktemp(suffix='.db')
        db = sqlite3.connect(path)
        db.execute('CREATE TABLE orders (id INTEGER PRIMARY KEY, sku TEXT, qty INTEGER)')
        db.executemany('INSERT INTO orders (sku, qty) VALUES (?, ?)',
                       [(f'sku-{i}', i) for i in range(50)])
        db.commit()
        db.close()
        with open(path, 'rb') as handle:
            blob = handle.read()
        import os as _os

        _os.unlink(path)
        self.doc = Document.objects.create(
            user=self.user, folder=None, name='shop.db', file_type='other',
            file_size=len(blob), content_text='', status='stored')
        self.doc.file.save('shop.db', ContentFile(blob))
        self.conn = DataConnection.objects.create(
            user=self.user, kind='sqlite', name='Shop', vfs_path='/shop.db')

    def tearDown(self):
        self._override.disable()
        shutil.rmtree(self._media, ignore_errors=True)

    def call(self, name, args, **extra):
        ctx = {'user_id': self.user.id, 'file_scope': self.scope, **extra}
        return json.loads(async_to_sync(execute_tool)(name, args, ctx))

    def test_query_reads_rows(self):
        out = self.call('query_sql', {
            'connection': self.conn.id,
            'sql': 'SELECT sku, qty FROM orders WHERE qty > %s' % 48})
        self.assertEqual(out['row_count'], 1)
        self.assertEqual(out['columns'], ['sku', 'qty'])

    def test_params_are_bound_not_pasted(self):
        out = self.call('query_sql', {
            'connection': self.conn.id,
            'sql': 'SELECT qty FROM orders WHERE sku = ?', 'params': ['sku-7']})
        self.assertEqual(out['rows'], [[7]])

    def test_a_write_is_refused_on_a_read_connection(self):
        out = self.call('execute_sql', {
            'connection': self.conn.id, 'sql': 'DELETE FROM orders'})
        self.assertIn('does not allow writes', out['error'])

    def test_a_write_runs_where_allowed(self):
        self.conn.allow_write = True
        self.conn.save(update_fields=['allow_write'])
        out = self.call('execute_sql', {
            'connection': self.conn.id,
            'sql': 'UPDATE orders SET qty = 0 WHERE sku = ?',
            'params': ['sku-1']})
        self.assertIn('1 row(s) affected', out['rendered'])

    def test_unselected_connections_are_refused(self):
        out = self.call('query_sql', {
            'connection': self.conn.id, 'sql': 'SELECT 1'},
            data_connections=[])
        self.assertIn('not selected', out['error'])

    def test_foreign_connections_are_never_offered(self):
        other = User.objects.create_user('other', 'o@example.com', 'pw')
        out = self.call('query_sql', {'connection': self.conn.id, 'sql': 'SELECT 1'})
        self.assertNotIn('error', out)
        ctx = {'user_id': other.id, 'file_scope': vfs.chat_scope(other)}
        out = json.loads(async_to_sync(execute_tool)(
            'query_sql', {'connection': self.conn.id, 'sql': 'SELECT 1'}, ctx))
        self.assertIn('belongs to this user', out['error'])

    def test_big_results_land_as_a_csv(self):
        out = self.call('query_sql', {
            'connection': self.conn.id, 'sql': 'SELECT * FROM orders'})
        self.assertNotIn('error', out)
        out = self.call('query_sql', {
            'connection': self.conn.id,
            'sql': 'WITH RECURSIVE c(x) AS '
                   '(SELECT 1 UNION ALL SELECT x+1 FROM c LIMIT 1500) '
                   'SELECT x FROM c'})
        self.assertIn('csv_path', out)
        self.assertIn('1,500 rows', out['rendered'])

    def test_describe_lists_tables_and_columns(self):
        out = self.call('describe_schema', {'connection': self.conn.id})
        self.assertIn('orders', out['tables'])
        out = self.call('describe_schema', {'connection': self.conn.id, 'table': 'orders'})
        self.assertEqual([c['name'] for c in out['columns']], ['id', 'sku', 'qty'])


class ApiCallerTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('caller', 'c@example.com', 'pw')
        self.conn = ApiConnection.objects.create(
            user=self.user, name='Shop API', base_url='https://shop.example.com',
            openapi_spec={'paths': {
                '/v1/orders': {'get': {'summary': 'List orders'}},
                '/v1/orders/{id}': {'post': {'summary': 'Create order'}},
            }},
            allowed_methods=['GET', 'POST'])
        self.ctx = {'user_id': self.user.id}

    def call(self, name, args, **extra):
        ctx = dict(self.ctx)
        ctx.update(extra)
        return json.loads(async_to_sync(execute_tool)(name, args, ctx))

    def test_operations_are_indexed_not_dumped(self):
        out = self.call('list_api_operations', {'connection': self.conn.id})
        self.assertEqual(len(out['connections'][0]['operations']), 2)

    def test_an_unknown_path_names_its_neighbours(self):
        out = self.call('call_api', {
            'connection': self.conn.id, 'method': 'GET', 'path': '/v1/ordrs'},
            api_connections={self.conn.id: 'all'})
        self.assertIn('/v1/orders', out['error'])

    def test_a_disallowed_method_is_refused(self):
        out = self.call('call_api', {
            'connection': self.conn.id, 'method': 'DELETE', 'path': '/v1/orders'},
            api_connections={self.conn.id: 'all'})
        self.assertIn('not allowed', out['error'])

    def test_read_mode_refuses_writes(self):
        out = self.call('call_api', {
            'connection': self.conn.id, 'method': 'POST', 'path': '/v1/orders'},
            api_connections={self.conn.id: 'read'})
        self.assertIn('read-only', out['error'])

    def test_no_scope_means_no_calls(self):
        out = self.call('call_api', {
            'connection': self.conn.id, 'method': 'GET', 'path': '/v1/orders'},
            api_connections={})
        self.assertIn('not selected', out['error'])

    def test_ssrf_hosts_are_refused(self):
        evil = ApiConnection.objects.create(
            user=self.user, name='Evil', base_url='http://169.254.169.254/')
        out = self.call('call_api', {
            'connection': evil.id, 'method': 'GET', 'path': '/'})
        self.assertIn('blocked', out['error'].lower())

    def test_a_live_get_round_trip(self):
        server = _StubServer()
        server.start()
        self.addCleanup(server.stop)
        conn = ApiConnection.objects.create(
            user=self.user, name='Local', base_url=server.url)
        with mock.patch('core.safety.net.check_egress',
                        return_value=(True, '')):
            out = self.call('call_api', {
                'connection': conn.id, 'method': 'GET', 'path': '/v1/orders'})
        self.assertEqual(out['data'], {'orders': [1, 2]})


class _StubServer:
    def start(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'{"orders": [1, 2]}'
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
