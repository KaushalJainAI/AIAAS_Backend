import aiohttp
import logging
from django.conf import settings
from urllib.parse import urlencode, unquote

logger = logging.getLogger(__name__)

class GoogleOAuthProvider:
    """
    Handles Google OAuth2 interactions using aiohttp for async support.
    """
    AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    TOKEN_URL = "https://oauth2.googleapis.com/token"
    USER_INFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

    def __init__(self, redirect_uri=None):
        self.client_id = settings.GOOGLE_OAUTH_CLIENT_ID
        self.client_secret = settings.GOOGLE_OAUTH_CLIENT_SECRET
        self.redirect_uri = redirect_uri or settings.GOOGLE_OAUTH_REDIRECT_URI

    def get_auth_url(self, scopes=None, state=None, prompt='consent'):
        if not scopes:
             scopes = settings.GOOGLE_OAUTH_LOGIN_SCOPES
             
        params = {
            'client_id': self.client_id,
            'redirect_uri': self.redirect_uri,
            'response_type': 'code',
            'scope': ' '.join(scopes),
            'access_type': 'offline',
            'prompt': prompt, 
            'include_granted_scopes': 'true'
        }
        
        if state:
            params['state'] = state
            
        return f"{self.AUTHORIZATION_URL}?{urlencode(params)}"

    async def exchange_code(self, code):
        """
        Exchanges authorization code for access and refresh tokens.
        """
        data = {
            'code': unquote(code),
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'redirect_uri': self.redirect_uri,
            'grant_type': 'authorization_code'
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(self.TOKEN_URL, data=data) as response:
                return await response.json()

    async def refresh_token(self, refresh_token):
        """
        Refreshes an expired access token.
        """
        data = {
             'client_id': self.client_id,
             'client_secret': self.client_secret,
             'refresh_token': refresh_token,
             'grant_type': 'refresh_token'
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(self.TOKEN_URL, data=data) as response:
                return await response.json()

    async def get_user_info(self, access_token):
        """
        Fetches user profile information.
        """
        headers = {'Authorization': f'Bearer {access_token}'}
        async with aiohttp.ClientSession() as session:
            async with session.get(self.USER_INFO_URL, headers=headers) as response:
                return await response.json()
