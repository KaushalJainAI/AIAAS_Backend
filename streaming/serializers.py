from rest_framework import serializers

class StreamHistoryFilterSerializer(serializers.Serializer):
    """Serializer for execution history event stream parameters."""
    after_sequence = serializers.IntegerField(default=0, min_value=0)
    limit = serializers.IntegerField(default=100, min_value=1, max_value=500)
