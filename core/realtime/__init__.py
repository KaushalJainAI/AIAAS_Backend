"""
The websocket half of the request pipeline.

`channels_middleware` authenticates a socket the way `core.auth` authenticates a
request; `consumers` holds the connections. Separate from `core.http` because
Channels' scope is not a Django request and the two cannot share middleware.
"""
