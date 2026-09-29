import os
os.environ['DATABASE_URL'] = 'sqlite://'
os.environ['DEMO_ADMIN_PASSWORD'] = 'demo-local-only'
os.environ['DEMO_SELLER_PASSWORD'] = 'demo-local-only'
import unittest
from app import app, db, seed
from models import Producto


class DemoTests(unittest.TestCase):
    def setUp(self):
        app.config['TESTING'] = True
        self.context = app.app_context()
        self.context.push()
        db.drop_all()
        seed()
        self.client = app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def login(self, username):
        return self.client.post('/login', data={'username': username, 'password': 'demo-local-only'})

    def test_anonymous_inventory_requires_login(self):
        response = self.client.get('/inventario')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response.location)

    def test_admin_can_seed_only_synthetic_products(self):
        self.login('admin')
        self.assertEqual(self.client.post('/admin/seed-test').status_code, 302)
        self.assertEqual(Producto.query.count(), 12)
        self.assertTrue(all(product.nombre.startswith('Producto Demo ') for product in Producto.query.all()))
        self.assertEqual(self.client.get('/inventario').status_code, 200)

    def test_seed_is_idempotent(self):
        self.login('admin')
        self.client.post('/admin/seed-test')
        self.client.post('/admin/seed-test')
        self.assertEqual(Producto.query.count(), 12)

    def test_seller_cannot_manage_users(self):
        self.login('vendedor')
        self.assertEqual(self.client.get('/usuarios').status_code, 302)
        response = self.client.get('/pos')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith('/caja'))


if __name__ == '__main__':
    unittest.main()
