"""
Generate a VAPID keypair for Web Push (closed-browser OS notifications).

    python manage.py generate_vapid_keys

Prints `VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY` lines for `.env`. The public
key is served at GET /api/notifications/push/vapid-key/; the private key never
leaves the server. Uses `cryptography` (already a dependency) — no new package.
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Generate a VAPID EC P-256 keypair for Web Push.'

    def handle(self, *args, **options):
        import base64

        from cryptography.hazmat.primitives.asymmetric import ec

        def b64url(data: bytes) -> str:
            return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')

        private_key = ec.generate_private_key(ec.SECP256R1())
        private_numbers = private_key.private_numbers()
        public_numbers = private_numbers.public_numbers

        x = public_numbers.x.to_bytes(32, 'big')
        y = public_numbers.y.to_bytes(32, 'big')
        public_key = b64url(b'\x04' + x + y)
        private_key_b64 = b64url(private_numbers.private_value.to_bytes(32, 'big'))

        self.stdout.write(f"VAPID_PUBLIC_KEY={public_key}")
        self.stdout.write(f"VAPID_PRIVATE_KEY={private_key_b64}")
