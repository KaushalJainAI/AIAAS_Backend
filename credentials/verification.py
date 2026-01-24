import requests
import logging
import json
from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

class CredentialVerifier:
    """
    Handles verification logic for different credential types.
    Branches verification logic by auth_method.
    """
    
    @staticmethod
    def verify(credential, audit_context=None):
        """
        Dispatch verification based on credential type's auth_method.
        Returns (is_valid: bool, message: str)
        """
        auth_method = credential.credential_type.auth_method
        slug = credential.credential_type.slug
        
        # Get decrypted data
        try:
            data = credential.get_credential_data(**(audit_context or {}))
            # Merge public metadata so verification can access non-secret fields (like loginUrl)
            if credential.public_metadata:
                data.update(credential.public_metadata)
        except Exception as e:
            return False, f"Decryption failed: {str(e)}"
            
        logger.info(f"Verifying credential {credential.id} (Type: {slug}, Method: {auth_method})")

        try:
            if auth_method == 'api_key':
                return CredentialVerifier._verify_api_key(credential, data)
            elif auth_method == 'oauth2':
                return CredentialVerifier._verify_oauth2(credential, data)
            elif auth_method == 'bearer':
                return CredentialVerifier._verify_bearer(credential, data)
            elif auth_method == 'basic':
                return CredentialVerifier._verify_basic(credential, data)
            elif auth_method == 'custom':
                return CredentialVerifier._verify_custom(credential, data)
            else:
                return False, f"Unknown auth method: {auth_method}"
                
        except Exception as e:
            logger.error(f"Verification crashed for {slug}: {str(e)}", exc_info=True)
            return False, f"Internal Verification Error: {str(e)}"

    @staticmethod
    def _verify_api_key(credential, data):
        """
        Verify API Key credentials.
        Strategy: Make a test request if known, or validate fields.
        """
        slug = credential.credential_type.slug
        
        # 1. Specific Handlers
        if slug == 'openai':
            api_key = data.get('apiKey') or data.get('api_key')
            if not api_key:
                return False, "Missing apiKey field"
            
            base_url = data.get('baseUrl') or data.get('base_url') or 'https://api.openai.com/v1'
            base_url = base_url.rstrip('/')
            
            try:
                response = requests.get(
                    f"{base_url}/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=10
                )
                if response.status_code == 200:
                    return True, "Successfully connected to OpenAI"
                elif response.status_code == 401:
                    return False, "Invalid API Key"
                else:
                    return False, f"OpenAI returned status {response.status_code}"
            except requests.exceptions.RequestException as e:
                return False, f"Network error: {str(e)}"

        # 2. Generic Fallback: Check if required fields exist
        schema = credential.credential_type.fields_schema
        missing = []
        for field in schema:
            if field.get('required') and not data.get(field['name']):
                missing.append(field['name'])
        
        if missing:
             return False, f"Missing required fields: {', '.join(missing)}"
             
        return True, "API Key format looks valid (No test endpoint configured)"

    @staticmethod
    def _verify_bearer(credential, data):
        """
        Verify Bearer Token credentials.
        """
        slug = credential.credential_type.slug
        
        # 1. Specific Handlers
        if slug == 'slack':
            token = data.get('token') or data.get('api_token') or data.get('bot_token')
            if not token:
                return False, "Missing token field"
                
            try:
                # Slack calls this a "token" but passes it as Bearer
                response = requests.post(
                    "https://slack.com/api/auth.test",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=10
                )
                if response.status_code != 200:
                    return False, f"Slack API returned {response.status_code}"
                
                res_json = response.json()
                if res_json.get('ok'):
                    return True, f"Connected as {res_json.get('user')} in {res_json.get('team')}"
                else:
                    return False, f"Slack Error: {res_json.get('error')}"
            except Exception as e:
                return False, f"Network error: {str(e)}"
                
        # 2. Generic Check
        token = data.get('token')
        if not token:
             return False, "Missing 'token' field"
             
        return True, "Token format valid (No test endpoint configured)"

    @staticmethod
    def _verify_basic(credential, data):
        """
        Verify Basic Auth credentials.
        """
        username = data.get('username') or data.get('user')
        password = data.get('password') or data.get('pass')
        
        if not username or not password:
            return False, "Missing username or password"
            
        # TODO: If we had a target URL in the credential type config, we could text it.
        return True, "Basic Auth format valid (No test endpoint configured)"

    @staticmethod
    def _verify_oauth2(credential, data):
        """
        Verify OAuth2 credentials.
        Uses stored access token to check validity.
        """
        # Validate Config
        oauth_config = credential.credential_type.oauth_config or {}
        auth_url = oauth_config.get('auth_url')
        token_url = oauth_config.get('token_url')
        
        if not auth_url or not token_url:
             return False, f"Invalid Configuration: Missing OAuth2 setup for {credential.credential_type.name}"

        # Get Access Token
        access_token = credential.get_valid_access_token()
        
        if not access_token:
            return False, "No valid access token available. Please reconnect."
            
        slug = credential.credential_type.slug
        
        # Google Special Case
        if slug == 'google-oauth2':
            try:
                # Call userinfo
                resp = requests.get(
                    "https://www.googleapis.com/oauth2/v1/userinfo",
                    params={"access_token": access_token},
                    timeout=10
                )
                if resp.status_code == 200:
                    email = resp.json().get('email')
                    return True, f"Verified Google Account: {email}"
                elif resp.status_code == 401:
                    return False, "Access Token Expired/Invalid"
                else:
                    return False, f"Google API Error: {resp.status_code}"
            except Exception as e:
                return False, f"Verification Request Failed: {str(e)}"
                
        # Generic OAuth check (if we knew a generic 'me' endpoint)
        # Lacking a standard 'test_url' in oauth_config, we assume success if we have a valid token.
        return True, "OAuth2 Token present and nominally valid"

    @staticmethod
    def _verify_custom(credential, data):
        """
        Verify Custom credentials.
        """
        slug = credential.credential_type.slug
        
        if slug == 'postgres':
            return CredentialVerifier._verify_postgres(data)
            
        elif slug == 'website-login':
             # Use the Selenium browser flow
             return CredentialVerifier._verify_website_login(data, credential)
             
        # Fallback
        return False, f"No custom verification logic defined for {slug}"

    @staticmethod
    def _verify_postgres(data):
        try:
            import psycopg2
        except ImportError:
            return False, "psycopg2 not installed on server"
            
        try:
            conn = psycopg2.connect(
                host=data.get('host'),
                port=data.get('port'),
                dbname=data.get('database'),
                user=data.get('username'),
                password=data.get('password'),
                connect_timeout=5
            )
            conn.close()
            return True, "Successfully connected to PostgreSQL"
        except Exception as e:
            return False, f"Connection failed: {str(e)}"

    @staticmethod
    def _verify_website_login(data, credential_instance=None):
        """
        Specific validator for 'website-login' using Selenium.
        """
        from .browser_utils import login_and_extract_tokens
        
        url = data.get('loginUrl') or data.get('login_url')
        username = data.get('username') or data.get('user_name') or data.get('email')
        password = data.get('password')
        
        if not url or not username or not password:
            return False, "Missing login fields"
            
        try:
            logger.info(f"Running browser verification for {url}")
            tokens = login_and_extract_tokens(url, username, password)
            if tokens:
                found_keys = ", ".join(tokens.keys())
                
                if credential_instance:
                    updated_data = data.copy()
                    updated_data['extracted_auth'] = tokens
                    credential_instance.set_credential_data(updated_data)
                    
                    # Store standard JWT/OAuth style tokens if found
                    extracted_access = None
                    extracted_refresh = None
                    for k, v in tokens.items():
                        if 'access_token' in k.lower() or 'accesstoken' in k.lower():
                            extracted_access = v
                        if 'refresh_token' in k.lower() or 'refreshtoken' in k.lower():
                            extracted_refresh = v
                    
                    if extracted_access or extracted_refresh:
                        fernet = Fernet(credential_instance._get_encryption_key())
                        if extracted_access:
                            credential_instance.access_token = fernet.encrypt(extracted_access.encode())
                        if extracted_refresh:
                             credential_instance.refresh_token = fernet.encrypt(extracted_refresh.encode())
                    
                    credential_instance.save()
                    
                return True, f"Login successful. Tokens found: {found_keys}"
            return False, "Login completed but no auth tokens were found. Please check if the credentials are correct."
        except Exception as e:
            # Check for common selenium errors
            msg = str(e)
            if "Could not find username field" in msg:
                return False, "Could not locate username field on the page. Please verify the URL."
            if "Could not find password field" in msg:
                return False, "Could not locate password field on the page."
            return False, f"Browser Verification Failed: {msg}"
