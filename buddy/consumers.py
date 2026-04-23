import json
from channels.generic.websocket import AsyncWebsocketConsumer

class BuddyConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        # We can use the user id as a group name to allow targeted control
        self.user = self.scope.get("user")
        if self.user and self.user.is_authenticated:
            self.group_name = f"buddy_{self.user.id}"
            
            # Join group
            await self.channel_layer.group_add(
                self.group_name,
                self.channel_name
            )
            await self.accept()
        else:
            await self.close()

    async def disconnect(self, close_code):
        if hasattr(self, 'group_name'):
            # Leave group
            await self.channel_layer.group_discard(
                self.group_name,
                self.channel_name
            )

    async def receive(self, text_data):
        """
        Handle incoming messages from the frontend (e.g., context updates).
        """
        data = json.loads(text_data)
        message_type = data.get('type')
        
        if message_type == 'context_update':
            # Handle context update (e.g., screen content)
            context = data.get('context', {})
            
            # Save context to cache so the LLM can read it
            from django.core.cache import cache
            cache.set(f"buddy_context_{self.user.id}", context, timeout=3600)
            
            # Echo for now
            await self.send(text_data=json.dumps({
                'type': 'status',
                'message': 'Context updated'
            }))
            
        elif message_type == 'request_action':
            # Handle a request for an action (if the frontend asks the assistant for help)
            # This is where we'd call the AI logic
            pass

    async def trigger_action(self, event):
        """
        Handle action trigger events sent from the backend logic/views.
        """
        action = event['action']
        params = event.get('parameters', {})
        
        # Send action to the frontend
        await self.send(text_data=json.dumps({
            'type': 'trigger_action',
            'action': action,
            'parameters': params
        }))
