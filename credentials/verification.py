import requests
import logging
import json

logger = logging.getLogger(__name__)

class CredentialVerifier:
    """
    Handles verification logic for different credential types.
    """
    
    @staticmethod
    def verify(credential):
        """
        Dispatch verification based on credential type slug.
        Returns (is_valid: bool, message: str)
        """
        slug = credential.credential_type.slug
        data = credential.get_credential_data()
        
        logger.info(f"Verifying credential {credential.id} of type {slug}")

        try:
            if slug == 'openai':
                return CredentialVerifier._verify_openai(data)
            elif slug == 'slack':
                return CredentialVerifier._verify_slack(data)
            elif slug == 'postgres':
                return CredentialVerifier._verify_postgres(data)
            elif slug == 'google-oauth2':
                return False, "Google OAuth2 verification requires browser flow. Please use the Connect button."
            elif slug == 'website-login':
                return False, "Website login verification requires a specific target URL logic."
            else:
                # Generic fallback if 'verify_url' is in headers or config (future)
                return False, f"Verification logic not implemented for {slug}"
                
        except Exception as e:
            logger.error(f"Verification crashed for {slug}: {str(e)}", exc_info=True)
            return False, f"Internal Verification Error: {str(e)}"

    @staticmethod
    def _verify_openai(data):
        api_key = data.get('apiKey')
        if not api_key:
            return False, "Missing apiKey field"
        
        base_url = data.get('baseUrl') or 'https://api.openai.com/v1'
        base_url = base_url.rstrip('/')
        
        try:
            # Simple metadata call to check key validity
            response = requests.get(
                f"{base_url}/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=10
            )
            
            if response.status_code == 200:
                return True, "Successfully connected to OpenAI"
            elif response.status_code == 401:
                return False, "Invalid API Key"
            elif response.status_code == 403:
                return False, "API Key does not have permission"
            else:
                return False, f"OpenAI returned status {response.status_code}"
                
        except requests.exceptions.RequestException as e:
            return False, f"Network error: {str(e)}"

    @staticmethod
    def _verify_slack(data):
        token = data.get('token')
        if not token:
            return False, "Missing token field"
            
        try:
            response = requests.post(
                "https://slack.com/api/auth.test",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10
            )
            
            if response.status_code != 200:
                return False, f"Slack API returned {response.status_code}"
                
            res_json = response.json()
            if res_json.get('ok'):
                user = res_json.get('user', 'Unknown')
                team = res_json.get('team', 'Unknown')
                return True, f"Connected as {user} in {team}"
            else:
                error = res_json.get('error', 'Unknown Error')
                return False, f"Slack Error: {error}"
                
        except requests.exceptions.RequestException as e:
             return False, f"Network error: {str(e)}"

    @staticmethod
    def _verify_postgres(data):
        # Requires psycopg2 or similar
        try:
            import psycopg2
        except ImportError:
            return False, "psycopg2 not installed on server"
            
        host = data.get('host')
        port = data.get('port')
        dbname = data.get('database')
        user = data.get('username')
        password = data.get('password')
        
        try:
            conn = psycopg2.connect(
                host=host,
                port=port,
                dbname=dbname,
                user=user,
                password=password,
                connect_timeout=5
            )
            conn.close()
            return True, "Successfully connected to PostgreSQL"
        except Exception as e:
            return False, f"Connection failed: {str(e)}"
