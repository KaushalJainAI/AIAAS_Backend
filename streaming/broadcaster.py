"""
SSE Broadcaster - Server-Sent Events for Real-time Execution Updates

Provides streaming functionality for:
- Execution progress updates
- Node execution events
- HITL request notifications
- Error notifications

Usage:
    broadcaster = SSEBroadcaster()
    
    # From executor
    await broadcaster.send_event(execution_id, 'node_start', data)
    
    # In view
    async def stream_view():
        async for event in broadcaster.stream_execution(execution_id):
            yield event
"""
import asyncio
import json
import logging
from datetime import datetime
from typing import AsyncGenerator, Optional
from uuid import UUID
from dataclasses import dataclass, asdict

from django.core.cache import cache
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

logger = logging.getLogger(__name__)


@dataclass
class StreamEvent:
    """Server-Sent Event data structure."""
    event_type: str
    data: dict
    id: Optional[str] = None
    retry: Optional[int] = None
    timestamp: str = None
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.utcnow().isoformat()
    
    def format_sse(self) -> str:
        """Format as SSE message."""
        lines = []
        
        if self.id:
            lines.append(f"id: {self.id}")
        
        lines.append(f"event: {self.event_type}")
        
        data_dict = {
            'type': self.event_type,
            'data': self.data,
            'timestamp': self.timestamp,
        }
        lines.append(f"data: {json.dumps(data_dict)}")
        
        if self.retry:
            lines.append(f"retry: {self.retry}")
        
        # SSE messages end with double newline
        return '\n'.join(lines) + '\n\n'


class SSEBroadcaster:
    """
    Server-Sent Events broadcaster for execution updates.
    
    Supports multi-instance deployment via Redis pub/sub (when configured).
    For single instance, uses in-memory queues.
    """
    
    # Event types
    EVENT_WORKFLOW_START = 'workflow_start'
    EVENT_WORKFLOW_COMPLETE = 'workflow_complete'
    EVENT_WORKFLOW_ERROR = 'workflow_error'
    EVENT_NODE_START = 'node_start'
    EVENT_NODE_COMPLETE = 'node_complete'
    EVENT_NODE_ERROR = 'node_error'
    EVENT_HITL_REQUEST = 'hitl_request'
    EVENT_PROGRESS = 'progress'
    EVENT_LOG = 'log'
    
    # In-memory subscribers (for single instance)
    # Format: {execution_id: [asyncio.Queue, ...]}
    _subscribers: dict[str, list[asyncio.Queue]] = {}
    _lock = asyncio.Lock()
    
    def __init__(self):
        self._channel_layer = None
    
    @property
    def channel_layer(self):
        """Get Django Channels layer (lazy load)."""
        if self._channel_layer is None:
            try:
                self._channel_layer = get_channel_layer()
            except Exception:
                self._channel_layer = None
        return self._channel_layer
    
    async def send_event(
        self,
        execution_id: str | UUID,
        event_type: str,
        data: dict,
        event_id: Optional[str] = None
    ):
        """
        Send an event to all subscribers of an execution.
        
        Args:
            execution_id: UUID of the execution
            event_type: Type of event (e.g., 'node_start')
            data: Event payload
            event_id: Optional event ID for client replay
        """
        execution_id = str(execution_id)
        event = StreamEvent(
            event_type=event_type,
            data=data,
            id=event_id,
        )
        
        # Try Channels first (for multi-instance)
        if self.channel_layer:
            try:
                await self.channel_layer.group_send(
                    f"execution_{execution_id}",
                    {
                        "type": "execution.event",
                        "event": asdict(event),
                    }
                )
                return
            except Exception as e:
                logger.warning(f"Channel layer send failed: {e}")
        
        # Fallback to in-memory queue
        await self._send_to_memory_subscribers(execution_id, event)
    
    async def _send_to_memory_subscribers(self, execution_id: str, event: StreamEvent):
        """Send event to in-memory subscribers."""
        async with self._lock:
            subscribers = self._subscribers.get(execution_id, [])
            dead_queues = []
            
            for queue in subscribers:
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    logger.warning(f"Queue full for execution {execution_id}")
                except Exception:
                    dead_queues.append(queue)
            
            # Remove dead queues
            for queue in dead_queues:
                subscribers.remove(queue)
    
    async def subscribe(self, execution_id: str | UUID) -> asyncio.Queue:
        """
        Subscribe to events for an execution.
        
        Returns an asyncio.Queue that will receive events.
        """
        execution_id = str(execution_id)
        queue = asyncio.Queue(maxsize=100)
        
        async with self._lock:
            if execution_id not in self._subscribers:
                self._subscribers[execution_id] = []
            self._subscribers[execution_id].append(queue)
        
        return queue
    
    async def unsubscribe(self, execution_id: str | UUID, queue: asyncio.Queue):
        """Unsubscribe from an execution's events."""
        execution_id = str(execution_id)
        
        async with self._lock:
            if execution_id in self._subscribers:
                try:
                    self._subscribers[execution_id].remove(queue)
                except ValueError:
                    pass
                
                # Clean up empty lists
                if not self._subscribers[execution_id]:
                    del self._subscribers[execution_id]
    
    async def stream_execution(
        self,
        execution_id: str | UUID,
        timeout: float = 300,  # 5 minutes
        heartbeat_interval: float = 30
    ) -> AsyncGenerator[str, None]:
        """
        Stream events for an execution as formatted SSE messages.
        
        Args:
            execution_id: UUID of the execution
            timeout: Maximum streaming duration in seconds
            heartbeat_interval: Seconds between heartbeat messages
            
        Yields:
            Formatted SSE message strings
        """
        execution_id = str(execution_id)
        queue = await self.subscribe(execution_id)
        
        try:
            # Send initial connection event
            yield StreamEvent(
                event_type='connected',
                data={'execution_id': execution_id}
            ).format_sse()
            
            end_time = asyncio.get_event_loop().time() + timeout
            last_heartbeat = asyncio.get_event_loop().time()
            
            while asyncio.get_event_loop().time() < end_time:
                try:
                    # Wait for event with heartbeat timeout
                    event = await asyncio.wait_for(
                        queue.get(),
                        timeout=heartbeat_interval
                    )
                    
                    yield event.format_sse()
                    
                    # Check for terminal events
                    if event.event_type in (
                        self.EVENT_WORKFLOW_COMPLETE,
                        self.EVENT_WORKFLOW_ERROR
                    ):
                        break
                        
                except asyncio.TimeoutError:
                    # Send heartbeat
                    current_time = asyncio.get_event_loop().time()
                    if current_time - last_heartbeat >= heartbeat_interval:
                        yield StreamEvent(
                            event_type='heartbeat',
                            data={}
                        ).format_sse()
                        last_heartbeat = current_time
                        
        finally:
            await self.unsubscribe(execution_id, queue)
    
    # Convenience methods for common events
    
    async def workflow_started(
        self,
        execution_id: str,
        workflow_id: int,
        workflow_name: str
    ):
        """Send workflow started event."""
        await self.send_event(
            execution_id,
            self.EVENT_WORKFLOW_START,
            {
                'workflow_id': workflow_id,
                'workflow_name': workflow_name,
                'status': 'running',
            }
        )
    
    async def workflow_completed(
        self,
        execution_id: str,
        output: dict,
        duration_ms: int
    ):
        """Send workflow completed event."""
        await self.send_event(
            execution_id,
            self.EVENT_WORKFLOW_COMPLETE,
            {
                'output': output,
                'duration_ms': duration_ms,
                'status': 'completed',
            }
        )
    
    async def workflow_error(
        self,
        execution_id: str,
        error: str,
        node_id: Optional[str] = None
    ):
        """Send workflow error event."""
        await self.send_event(
            execution_id,
            self.EVENT_WORKFLOW_ERROR,
            {
                'error': error,
                'node_id': node_id,
                'status': 'failed',
            }
        )
    
    async def node_started(
        self,
        execution_id: str,
        node_id: str,
        node_type: str,
        node_name: str
    ):
        """Send node started event."""
        await self.send_event(
            execution_id,
            self.EVENT_NODE_START,
            {
                'node_id': node_id,
                'node_type': node_type,
                'node_name': node_name,
                'status': 'running',
            }
        )
    
    async def node_completed(
        self,
        execution_id: str,
        node_id: str,
        output_preview: Optional[dict] = None,
        duration_ms: int = 0
    ):
        """Send node completed event."""
        await self.send_event(
            execution_id,
            self.EVENT_NODE_COMPLETE,
            {
                'node_id': node_id,
                'output_preview': output_preview,
                'duration_ms': duration_ms,
                'status': 'completed',
            }
        )
    
    async def node_error(
        self,
        execution_id: str,
        node_id: str,
        error: str
    ):
        """Send node error event."""
        await self.send_event(
            execution_id,
            self.EVENT_NODE_ERROR,
            {
                'node_id': node_id,
                'error': error,
                'status': 'failed',
            }
        )
    
    async def hitl_request(
        self,
        execution_id: str,
        request_id: str,
        request_type: str,
        title: str,
        message: str,
        options: Optional[list] = None
    ):
        """Send HITL request event."""
        await self.send_event(
            execution_id,
            self.EVENT_HITL_REQUEST,
            {
                'request_id': request_id,
                'request_type': request_type,
                'title': title,
                'message': message,
                'options': options or [],
            }
        )
    
    async def progress_update(
        self,
        execution_id: str,
        current_node: int,
        total_nodes: int,
        message: str = ''
    ):
        """Send progress update event."""
        await self.send_event(
            execution_id,
            self.EVENT_PROGRESS,
            {
                'current': current_node,
                'total': total_nodes,
                'percentage': int((current_node / total_nodes) * 100) if total_nodes > 0 else 0,
                'message': message,
            }
        )


# Singleton broadcaster instance
_broadcaster: Optional[SSEBroadcaster] = None


def get_broadcaster() -> SSEBroadcaster:
    """Get the global SSE broadcaster instance."""
    global _broadcaster
    if _broadcaster is None:
        _broadcaster = SSEBroadcaster()
    return _broadcaster


# Sync wrapper for use in sync code (e.g., Django views)
def send_event_sync(
    execution_id: str,
    event_type: str,
    data: dict,
    event_id: Optional[str] = None
):
    """Synchronous wrapper for sending events."""
    broadcaster = get_broadcaster()
    async_to_sync(broadcaster.send_event)(execution_id, event_type, data, event_id)
